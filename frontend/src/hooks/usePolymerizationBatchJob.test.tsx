// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BATCH_HISTORY_KEY, usePolymerizationBatchJob } from "./usePolymerizationBatchJob";
import type { BatchJob } from "../types/polymerizationBatch";

const fetchJob = vi.hoisted(() => vi.fn());
vi.mock("../services/polymerizationBatchApi", () => ({ fetchBatchJob: fetchJob }));
const first = "a".repeat(32), second = "b".repeat(32);
function job(id: string): BatchJob {
  return { job_id: id, status: "completed", stage: "finished", target_class: "polyimide",
    summary: { raw_pairs: 0, valid_pairs: 0, unique_pairs: 0, processed_pairs: 0, computed_unique_pairs: 0, candidate_count: 0,
      tables: { a: { row_count: 0, valid_rows: 0, unique_count: 0, duplicate_rows: 0, invalid_rows: 0, blank_rows: 0 }, b: { row_count: 0, valid_rows: 0, unique_count: 0, duplicate_rows: 0, invalid_rows: 0, blank_rows: 0 } } },
    artifacts: {}, created_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-10T00:00:00Z", finished_at: null, expires_at: null, error_code: null, message: null };
}
beforeEach(() => {
  fetchJob.mockReset();
  localStorage.clear();
  window.history.replaceState(null, "", `/monomer-polymerization?mode=batch&job_id=${first}`);
});
afterEach(() => cleanup());

describe("batch task identity", () => {
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
    expect(JSON.parse(localStorage.getItem(BATCH_HISTORY_KEY)!)).toEqual([second]);
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
