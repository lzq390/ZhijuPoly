// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MonomerPolymerizationRequest,
  MonomerPolymerizationResponse,
  MonomerPolymerizationStatusResponse
} from "../types";
import { useMonomerPolymerization } from "./useMonomerPolymerization";

const apiMocks = vi.hoisted(() => ({
  fetchStatus: vi.fn(),
  run: vi.fn()
}));

vi.mock("../services/api", () => ({
  fetchMonomerPolymerizationStatus: apiMocks.fetchStatus,
  runMonomerPolymerization: apiMocks.run
}));

const status: MonomerPolymerizationStatusResponse = {
  enabled: true,
  available: true,
  default_target_class: "polyether",
  available_target_classes: ["polyether"],
  max_results_limit: 20,
  message: "ready"
};

const request: MonomerPolymerizationRequest = {
  monomer_a_smiles: "CCO",
  monomer_b_smiles: null,
  target_class: "polyether",
  max_results: 5
};

function response(smiles: string): MonomerPolymerizationResponse {
  return {
    input_monomers: [{ role: "monomer_a", input_smiles: smiles, canonical_smiles: smiles }],
    target_class: "polyether",
    query_time_ms: 1,
    total: 0,
    results: [],
    warnings: []
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

beforeEach(() => {
  apiMocks.fetchStatus.mockReset().mockResolvedValue(status);
  apiMocks.run.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useMonomerPolymerization", () => {
  it("cancels an older run and ignores both stale success and stale failure", async () => {
    const first = deferred<MonomerPolymerizationResponse>();
    const second = deferred<MonomerPolymerizationResponse>();
    const signals: AbortSignal[] = [];
    apiMocks.run
      .mockImplementationOnce((_request: MonomerPolymerizationRequest, signal: AbortSignal) => {
        signals.push(signal);
        return first.promise;
      })
      .mockImplementationOnce((_request: MonomerPolymerizationRequest, signal: AbortSignal) => {
        signals.push(signal);
        return second.promise;
      });
    const { result } = renderHook(() => useMonomerPolymerization());
    await waitFor(() => expect(result.current.status).toEqual(status));

    let firstRun!: Promise<MonomerPolymerizationResponse | null>;
    let secondRun!: Promise<MonomerPolymerizationResponse | null>;
    act(() => { firstRun = result.current.run(request); });
    act(() => { secondRun = result.current.run({ ...request, monomer_a_smiles: "CCN" }); });
    expect(signals[0].aborted).toBe(true);

    await act(async () => {
      second.resolve(response("CCN"));
      await secondRun;
      first.resolve(response("CCO"));
      await firstRun;
    });
    expect(result.current.data?.input_monomers[0].input_smiles).toBe("CCN");
    expect(result.current.runError).toBeNull();
  });

  it("aborts status and run requests on unmount", async () => {
    const pendingStatus = deferred<MonomerPolymerizationStatusResponse>();
    const pendingRun = deferred<MonomerPolymerizationResponse>();
    let statusSignal!: AbortSignal;
    let runSignal!: AbortSignal;
    apiMocks.fetchStatus.mockImplementation((signal: AbortSignal) => {
      statusSignal = signal;
      return pendingStatus.promise;
    });
    apiMocks.run.mockImplementation((_request: MonomerPolymerizationRequest, signal: AbortSignal) => {
      runSignal = signal;
      return pendingRun.promise;
    });
    const { result, unmount } = renderHook(() => useMonomerPolymerization());
    act(() => { void result.current.run(request); });
    await waitFor(() => expect(runSignal).toBeTruthy());

    unmount();
    expect(statusSignal.aborted).toBe(true);
    expect(runSignal.aborted).toBe(true);
  });

  it("localizes network failures while preserving backend detail", async () => {
    apiMocks.run.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const first = renderHook(() => useMonomerPolymerization());
    await waitFor(() => expect(first.result.current.status).toEqual(status));
    await act(async () => { await first.result.current.run(request); });
    expect(first.result.current.runError).toBe("单体正向聚合请求失败，请检查网络或稍后重试。");
    first.unmount();

    apiMocks.run.mockRejectedValueOnce(new Error("后端 detail"));
    const second = renderHook(() => useMonomerPolymerization());
    await waitFor(() => expect(second.result.current.status).toEqual(status));
    await act(async () => { await second.result.current.run(request); });
    expect(second.result.current.runError).toBe("后端 detail");
    second.unmount();
  });
});
