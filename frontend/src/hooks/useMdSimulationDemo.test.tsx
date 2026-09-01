// @vitest-environment jsdom

import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MdDemoAtomDistanceResponse,
  MdDemoDefaultsResponse,
  MdDemoRunRequest,
  MdDemoRunResponse
} from "../types";
import { useMdSimulationDemo } from "./useMdSimulationDemo";

const apiMocks = vi.hoisted(() => ({
  defaults: vi.fn(),
  run: vi.fn(),
  distance: vi.fn()
}));

vi.mock("../services/api", () => ({
  fetchMdDemoDefaults: apiMocks.defaults,
  runMdDemo: apiMocks.run,
  calculateMdDemoAtomDistance: apiMocks.distance,
  isApiRequestError: (error: unknown, status?: number) =>
    error instanceof Error && "status" in error && (status === undefined || (error as Error & { status: number }).status === status)
}));

const request: MdDemoRunRequest = {
  smiles: "*CC*",
  temperature: 300,
  pressure: 1,
  n_atom: 1000,
  n_chain: 10,
  forcefield: "GAFF2_mod"
};

const defaults: MdDemoDefaultsResponse = {
  default_request: request,
  available_stages: [],
  summary: {
    primary_stage: "eq3",
    elapsed_seconds: 5,
    n_atoms: 20,
    n_frames: 10,
    n_chains: 2,
    final_density_g_cm3: 1,
    mean_temperature_k: 300,
    mean_total_energy_kcal_mol: 2
  },
  fixture_metadata: { fixture_version: 1, source: { label: "test", data_root_hint: "", generated_from: [] } }
};

function response(runId: string): MdDemoRunResponse {
  return {
    input: request,
    run_id: runId,
    status: "completed",
    query_time_ms: 2,
    stages: [],
    summary: defaults.summary,
    density_series: { key: "density", label: "Density", unit: "g/cm^3", points: [] },
    thermo_series: [],
    trajectory_preview: {
      stage_id: "eq3",
      frame_index: 0,
      time_ps: 0,
      sampled_points: 0,
      points: [],
      box: { lx: 1, ly: 1, lz: 1 }
    },
    atom_distance_series: null,
    fixture_metadata: defaults.fixture_metadata
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
  apiMocks.defaults.mockReset().mockResolvedValue(defaults);
  apiMocks.run.mockReset();
  apiMocks.distance.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useMdSimulationDemo", () => {
  it("forwards signals, cancels stale runs, and ignores their responses", async () => {
    const first = deferred<MdDemoRunResponse>();
    const second = deferred<MdDemoRunResponse>();
    const signals: AbortSignal[] = [];
    apiMocks.run
      .mockImplementationOnce((_payload: MdDemoRunRequest, signal: AbortSignal) => {
        signals.push(signal);
        return first.promise;
      })
      .mockImplementationOnce((_payload: MdDemoRunRequest, signal: AbortSignal) => {
        signals.push(signal);
        return second.promise;
      });
    const { result } = renderHook(() => useMdSimulationDemo({ resultRevealDelayMs: 0 }));
    await act(async () => { await Promise.resolve(); });

    let firstRun!: Promise<MdDemoRunResponse | null>;
    let secondRun!: Promise<MdDemoRunResponse | null>;
    act(() => { firstRun = result.current.run(request); });
    act(() => { secondRun = result.current.run({ ...request, smiles: "*CO*" }); });
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);

    await act(async () => {
      first.resolve(response("old"));
      second.resolve({ ...response("new"), input: { ...request, smiles: "*CO*" } });
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => { await Promise.all([firstRun, secondRun]); });
    expect(result.current.data?.run_id).toBe("new");
    expect(result.current.progress).toBe(100);
  });

  it("keeps the previous successful result when a later run fails", async () => {
    apiMocks.run.mockResolvedValueOnce(response("kept"));
    const { result } = renderHook(() => useMdSimulationDemo({ resultRevealDelayMs: 0 }));
    await act(async () => { await Promise.resolve(); });
    let firstRun!: Promise<MdDemoRunResponse | null>;
    act(() => { firstRun = result.current.run(request); });
    await act(async () => { await firstRun; });
    expect(result.current.data?.run_id).toBe("kept");

    apiMocks.run.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await act(async () => { await result.current.run({ ...request, pressure: 2 }); });
    expect(result.current.data?.run_id).toBe("kept");
    expect(result.current.runError).toBe("MD 模拟失败，请检查网络后重试。");
  });

  it("cancels distance work whenever the selection is cleared", async () => {
    const pending = deferred<MdDemoAtomDistanceResponse>();
    let signal!: AbortSignal;
    apiMocks.distance.mockImplementation((_payload: unknown, nextSignal: AbortSignal) => {
      signal = nextSignal;
      return pending.promise;
    });
    const { result } = renderHook(() => useMdSimulationDemo({ resultRevealDelayMs: 0 }));
    await act(async () => { await Promise.resolve(); });
    act(() => { void result.current.calculateDistance({ atom_id_1: 1, atom_id_2: 2, use_pbc: true }); });
    expect(result.current.distanceLoading).toBe(true);
    act(() => result.current.clearDistance());
    expect(signal.aborted).toBe(true);
    expect(result.current.distanceLoading).toBe(false);
  });
});
