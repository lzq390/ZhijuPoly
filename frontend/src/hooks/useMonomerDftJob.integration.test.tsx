// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MonomerDftCapabilitiesResponse,
  MonomerDftJobResponse,
  MonomerDftServiceStatusResponse
} from "../types";

const apiMocks = vi.hoisted(() => ({
  cancelJob: vi.fn(),
  createJob: vi.fn(),
  deleteArtifactsAndReload: vi.fn(),
  fetchCapabilities: vi.fn(),
  fetchJob: vi.fn(),
  fetchJobs: vi.fn(),
  fetchStatus: vi.fn()
}));

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    cancelMonomerDftJob: apiMocks.cancelJob,
    createMonomerDftJob: apiMocks.createJob,
    deleteMonomerDftArtifactsAndReloadJob: apiMocks.deleteArtifactsAndReload,
    fetchMonomerDftCapabilities: apiMocks.fetchCapabilities,
    fetchMonomerDftJob: apiMocks.fetchJob,
    fetchMonomerDftJobs: apiMocks.fetchJobs,
    fetchMonomerDftStatus: apiMocks.fetchStatus
  };
});

import { MonomerDftApiError } from "../services/api";
import { useMonomerDftJob } from "./useMonomerDftJob";

// Fake-time advances intentionally cross the production backoff windows.
vi.setConfig({ testTimeout: 300_000, hookTimeout: 300_000 });

const JOB_A = "188817d4-bdd2-4e25-9336-bfcbccc16a61";
const JOB_B = "288817d4-bdd2-4e25-9336-bfcbccc16a62";

const capabilities: MonomerDftCapabilitiesResponse = {
  enabled: true,
  available: true,
  schema_ready: true,
  calculation_types: ["single_point", "optimization"],
  properties: ["energy", "charges", "forces", "hessian", "frequencies"],
  default_model: "aimnet2",
  models: [],
  defaults: {
    conformer: { seed: 1, max_iterations: 500 },
    single_point: { properties: ["energy", "charges", "forces"] },
    optimization: { fmax_eV_per_A: 0.01, max_steps: 50, post_optimization_properties: [] }
  },
  limits: {
    min_optimization_steps: 10,
    max_optimization_steps: 50,
    max_concurrent_jobs: 1,
    max_queued_jobs: 8,
    max_active_jobs: 9
  }
};

const serviceStatus: MonomerDftServiceStatusResponse = {
  enabled: true,
  available: true,
  schema_ready: true,
  worker_status: "ok",
  runtime_ready: true,
  draining: false,
  active_jobs: 1,
  max_active_jobs: 9,
  message: "ready"
};

function makeJob(
  jobId: string,
  status: MonomerDftJobResponse["status"] = "running",
  overrides: Partial<MonomerDftJobResponse> = {}
): MonomerDftJobResponse {
  return {
    job_id: jobId,
    calculation_type: "single_point",
    status,
    request: {
      calculation_type: "single_point",
      input: { smiles: "CCO", net_charge: null, multiplicity: 1, psmiles_mode: null },
      model: "aimnet2",
      conformer: { seed: 1, max_iterations: 500 },
      single_point: { properties: ["energy"] }
    },
    request_sha256: "a".repeat(64),
    attempt: 1,
    queue_position: status === "queued" ? 1 : null,
    stage: status === "completed" ? "artifacts" : "single_point",
    progress_percent: status === "completed" ? 100 : 50,
    scientific_status: null,
    warnings: [],
    result: null,
    timings: {},
    provenance: {},
    error: null,
    artifacts: [],
    artifacts_state: "none",
    artifacts_deleted: false,
    cancel_requested: status === "cancel_requested",
    created_at: "2026-07-14T00:00:00Z",
    updated_at: "2026-07-14T00:00:01Z",
    started_at: null,
    finished_at: status === "completed" ? "2026-07-14T00:00:01Z" : null,
    idempotent_replay: false,
    ...overrides
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("useMonomerDftJob polling and operation fencing", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    Object.values(apiMocks).forEach((mock) => mock.mockReset());
    apiMocks.fetchStatus.mockResolvedValue(serviceStatus);
    apiMocks.fetchCapabilities.mockResolvedValue(capabilities);
    apiMocks.fetchJobs.mockResolvedValue({ items: [], page: 1, page_size: 20, total: 0 });
  });

  it("keeps retrying after more than three network failures and resets to polling on success", async () => {
    apiMocks.fetchJob
      .mockRejectedValueOnce(new TypeError("network-1"))
      .mockRejectedValueOnce(new TypeError("network-2"))
      .mockRejectedValueOnce(new TypeError("network-3"))
      .mockRejectedValueOnce(new TypeError("network-4"))
      .mockResolvedValue(makeJob(JOB_A));

    const { result, unmount } = renderHook(() => useMonomerDftJob({ initialJobId: JOB_A }));
    await flush();
    expect(result.current.pollState).toBe("degraded");

    await advance(1_500);
    await advance(3_000);
    await advance(6_000);
    expect(apiMocks.fetchJob).toHaveBeenCalledTimes(4);
    expect(result.current.pollState).toBe("degraded");

    await advance(10_000);
    expect(apiMocks.fetchJob).toHaveBeenCalledTimes(5);
    expect(result.current.job?.job_id).toBe(JOB_A);
    expect(result.current.pollState).toBe("polling");
    unmount();
  });

  it("does not overlap a pending job request", async () => {
    const first = deferred<MonomerDftJobResponse>();
    apiMocks.fetchJob.mockReturnValueOnce(first.promise).mockResolvedValue(makeJob(JOB_A));
    const { unmount } = renderHook(() => useMonomerDftJob({ initialJobId: JOB_A }));
    await flush();
    await advance(60_000);
    expect(apiMocks.fetchJob).toHaveBeenCalledTimes(1);

    first.resolve(makeJob(JOB_A));
    await flush();
    await advance(1_500);
    expect(apiMocks.fetchJob).toHaveBeenCalledTimes(2);
    unmount();
  });

  it("aborts the previous selection and the current selection on unmount", async () => {
    const signals: AbortSignal[] = [];
    apiMocks.fetchJob.mockImplementation((_jobId: string, signal: AbortSignal) => {
      signals.push(signal);
      return new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const { result, unmount } = renderHook(() => useMonomerDftJob({ initialJobId: JOB_A }));
    await flush();
    act(() => result.current.loadJob(JOB_B));
    await flush();
    expect(signals).toHaveLength(2);
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);
    unmount();
    expect(signals[1].aborted).toBe(true);
  });

  it("stops immediately on a non-retryable 404", async () => {
    apiMocks.fetchJob.mockRejectedValue(new MonomerDftApiError({
      message: "not found",
      status: 404,
      retryable: false
    }));
    const { result, unmount } = renderHook(() => useMonomerDftJob({ initialJobId: JOB_A }));
    await flush();
    expect(result.current.pollState).toBe("stopped");
    await advance(60_000);
    expect(apiMocks.fetchJob).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("fences a late cancellation for A after selecting B without starting another A poll", async () => {
    const cancellation = deferred<MonomerDftJobResponse>();
    apiMocks.fetchJob.mockImplementation((jobId: string) => Promise.resolve(makeJob(jobId)));
    apiMocks.cancelJob.mockReturnValue(cancellation.promise);
    const onJobIdChange = vi.fn();
    const { result, unmount } = renderHook(() => useMonomerDftJob({
      initialJobId: JOB_A,
      onJobIdChange
    }));
    await flush();

    act(() => { void result.current.cancel(); });
    await flush();
    expect(result.current.cancellingJobId).toBe(JOB_A);
    act(() => result.current.loadJob(JOB_B));
    await flush();
    expect(result.current.job?.job_id).toBe(JOB_B);

    cancellation.resolve(makeJob(JOB_A, "cancel_requested"));
    await flush();
    expect(result.current.job?.job_id).toBe(JOB_B);
    expect(result.current.cancellingJobId).toBeNull();
    expect(onJobIdChange).toHaveBeenLastCalledWith(JOB_B);
    expect(apiMocks.fetchJob.mock.calls.filter(([jobId]) => jobId === JOB_A)).toHaveLength(1);
    unmount();
  });

  it("stops polling a terminal job", async () => {
    apiMocks.fetchJob.mockResolvedValue(makeJob(JOB_A, "completed"));
    const { result, unmount } = renderHook(() => useMonomerDftJob({ initialJobId: JOB_A }));
    await flush();
    expect(result.current.pollState).toBe("terminal");
    await advance(60_000);
    expect(apiMocks.fetchJob).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("does not read history until the schema status and capabilities are ready", async () => {
    const status = deferred<MonomerDftServiceStatusResponse>();
    const nextCapabilities = deferred<MonomerDftCapabilitiesResponse>();
    const history = deferred<{ items: never[]; page: number; page_size: number; total: number }>();
    apiMocks.fetchStatus.mockReturnValue(status.promise);
    apiMocks.fetchCapabilities.mockReturnValue(nextCapabilities.promise);
    apiMocks.fetchJobs.mockReturnValue(history.promise);
    const { unmount } = renderHook(() => useMonomerDftJob());
    await flush();
    await advance(30_000);
    expect(apiMocks.fetchStatus).toHaveBeenCalledTimes(1);
    expect(apiMocks.fetchJobs).not.toHaveBeenCalled();
    status.resolve(serviceStatus);
    nextCapabilities.resolve(capabilities);
    await flush();
    expect(apiMocks.fetchJobs).toHaveBeenCalledTimes(1);
    history.resolve({ items: [], page: 1, page_size: 20, total: 0 });
    unmount();
  });

  it("aborts job and history work and clears stale state when schema readiness falls", async () => {
    const jobSignals: AbortSignal[] = [];
    apiMocks.fetchJob.mockImplementation((_jobId: string, signal: AbortSignal) => {
      jobSignals.push(signal);
      return new Promise((_resolve, reject) => {
        signal.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true }
        );
      });
    });
    apiMocks.fetchStatus
      .mockResolvedValueOnce(serviceStatus)
      .mockResolvedValue({ ...serviceStatus, schema_ready: false, available: false });
    apiMocks.fetchCapabilities
      .mockResolvedValueOnce(capabilities)
      .mockResolvedValue({ ...capabilities, schema_ready: false, available: false });
    const onJobIdChange = vi.fn();
    const { result, unmount } = renderHook(() => useMonomerDftJob({
      initialJobId: JOB_A,
      onJobIdChange
    }));
    await flush();
    expect(jobSignals).toHaveLength(1);
    expect(apiMocks.fetchJobs).toHaveBeenCalledTimes(1);

    await advance(30_000);
    await flush();
    expect(jobSignals[0].aborted).toBe(true);
    expect(result.current.serviceStatus?.schema_ready).toBe(false);
    expect(result.current.capabilities).toBeNull();
    expect(result.current.job).toBeNull();
    expect(result.current.history).toBeNull();
    expect(result.current.pollState).toBe("idle");
    expect(onJobIdChange).toHaveBeenLastCalledWith(null);
    expect(apiMocks.fetchJobs).toHaveBeenCalledTimes(1);
    unmount();
  });

  it("honors a false status even when the parallel capabilities request fails", async () => {
    const jobSignals: AbortSignal[] = [];
    apiMocks.fetchJob.mockImplementation((_jobId: string, signal: AbortSignal) => {
      jobSignals.push(signal);
      return new Promise((_resolve, reject) => {
        signal.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true }
        );
      });
    });
    apiMocks.fetchStatus
      .mockResolvedValueOnce(serviceStatus)
      .mockResolvedValue({ ...serviceStatus, schema_ready: false, available: false });
    apiMocks.fetchCapabilities
      .mockResolvedValueOnce(capabilities)
      .mockRejectedValue(new TypeError("capabilities unavailable"));
    const onJobIdChange = vi.fn();
    const { result, unmount } = renderHook(() => useMonomerDftJob({
      initialJobId: JOB_A,
      onJobIdChange
    }));
    await flush();
    expect(jobSignals).toHaveLength(1);

    await advance(30_000);
    await flush();

    expect(jobSignals[0].aborted).toBe(true);
    expect(result.current.serviceStatus?.schema_ready).toBe(false);
    expect(result.current.capabilities).toBeNull();
    expect(result.current.job).toBeNull();
    expect(result.current.history).toBeNull();
    expect(onJobIdChange).toHaveBeenLastCalledWith(null);
    unmount();
  });
});
