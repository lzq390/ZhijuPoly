// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MonomerMdJobResponse } from "../types";
import { useMonomerMdSimulation } from "./useMonomerMdSimulation";

const api = vi.hoisted(() => ({
  cancelMonomerMdJob: vi.fn(),
  createMonomerMdJob: vi.fn(),
  deleteMonomerMdArtifacts: vi.fn(),
  deleteMonomerMdJob: vi.fn(),
  fetchMonomerMdJob: vi.fn(),
  fetchMonomerMdJobs: vi.fn(),
  fetchMonomerMdProtocols: vi.fn(),
  fetchMonomerMdStatus: vi.fn()
}));

vi.mock("../services/api", () => api);

const firstId = "11111111111111111111111111111111";
const secondId = "22222222222222222222222222222222";

function completedJob(jobId: string): MonomerMdJobResponse {
  return {
    job_id: jobId,
    status: "completed",
    run_mode: "demo",
    protocol: "DensityDemo",
    progress_percent: 100,
    result: { summary: { n_steps: 300 }, artifacts: [] }
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchMonomerMdStatus.mockResolvedValue({ available: true, can_submit: true, formal_can_submit: true });
  api.fetchMonomerMdProtocols.mockResolvedValue({ enabled: true, available: true, protocols: [], message: "ready" });
  api.fetchMonomerMdJobs.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 10 });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useMonomerMdSimulation deep links", () => {
  it("restarts an aborted cold-start recovery request in StrictMode", async () => {
    let callCount = 0;
    api.fetchMonomerMdJob.mockImplementation((jobId: string, signal: AbortSignal) => {
      callCount += 1;
      if (callCount > 1) return Promise.resolve(completedJob(jobId));
      return new Promise((_, reject) => {
        signal.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        }, { once: true });
      });
    });

    const { result } = renderHook(
      () => useMonomerMdSimulation({ initialJobId: firstId }),
      { reactStrictMode: true }
    );

    await waitFor(() => expect(api.fetchMonomerMdJob).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.job?.job_id).toBe(firstId));
    expect(result.current.isJobLoading).toBe(false);
  });

  it("loads a cold-start deep link and follows a replacement id", async () => {
    api.fetchMonomerMdJob.mockImplementation((jobId: string) => Promise.resolve(completedJob(jobId)));
    const onJobIdChange = vi.fn();
    const { result, rerender } = renderHook(
      ({ jobId }) => useMonomerMdSimulation({ initialJobId: jobId, onJobIdChange }),
      { initialProps: { jobId: firstId as string | null } }
    );
    await waitFor(() => expect(result.current.job?.job_id).toBe(firstId));
    rerender({ jobId: secondId });
    await waitFor(() => expect(result.current.job?.job_id).toBe(secondId));
    expect(api.fetchMonomerMdJob).toHaveBeenCalledWith(firstId, expect.any(AbortSignal));
    expect(api.fetchMonomerMdJob).toHaveBeenCalledWith(secondId, expect.any(AbortSignal));
    expect(onJobIdChange).not.toHaveBeenCalled();
  });

  it("does not let an expired response overwrite the latest selected task", async () => {
    let resolveFirst: ((job: MonomerMdJobResponse) => void) | null = null;
    let resolveSecond: ((job: MonomerMdJobResponse) => void) | null = null;
    api.fetchMonomerMdJob.mockImplementation((jobId: string) => new Promise((resolve) => {
      if (jobId === firstId) resolveFirst = resolve;
      else resolveSecond = resolve;
    }));
    const { result, rerender } = renderHook(
      ({ jobId }) => useMonomerMdSimulation({ initialJobId: jobId }),
      { initialProps: { jobId: firstId } }
    );
    await waitFor(() => expect(api.fetchMonomerMdJob).toHaveBeenCalledTimes(1));
    rerender({ jobId: secondId });
    await waitFor(() => expect(api.fetchMonomerMdJob).toHaveBeenCalledTimes(2));
    await act(async () => { resolveSecond?.(completedJob(secondId)); });
    await waitFor(() => expect(result.current.job?.job_id).toBe(secondId));
    await act(async () => { resolveFirst?.(completedJob(firstId)); });
    expect(result.current.job?.job_id).toBe(secondId);
  });

  it("preserves the selected result and URL callback when creation fails before a new id", async () => {
    api.fetchMonomerMdJob.mockResolvedValue(completedJob(firstId));
    api.createMonomerMdJob.mockRejectedValue(new Error("capacity full"));
    const onJobIdChange = vi.fn();
    const { result } = renderHook(() => useMonomerMdSimulation({ initialJobId: firstId, onJobIdChange }));
    await waitFor(() => expect(result.current.job?.job_id).toBe(firstId));
    await act(async () => {
      await result.current.submit({ run_mode: "demo", protocol: "DensityDemo", smiles: "CCO" });
    });
    expect(result.current.job?.job_id).toBe(firstId);
    expect(result.current.data?.summary.n_steps).toBe(300);
    expect(result.current.error).toBe("capacity full");
    expect(onJobIdChange).not.toHaveBeenCalled();
  });

  it("changes the deep link only after the create response supplies a new id", async () => {
    api.createMonomerMdJob.mockResolvedValue({ job_id: secondId, status: "pending" });
    api.fetchMonomerMdJob.mockResolvedValue(completedJob(secondId));
    const onJobIdChange = vi.fn();
    const { result } = renderHook(() => useMonomerMdSimulation({ onJobIdChange }));
    await act(async () => {
      await result.current.submit({ run_mode: "demo", protocol: "DensityDemo", smiles: "CCO" });
    });
    expect(onJobIdChange).toHaveBeenCalledOnce();
    expect(onJobIdChange).toHaveBeenCalledWith(secondId);
    await waitFor(() => expect(result.current.job?.job_id).toBe(secondId));
  });
});
