// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { retireSession } from "../auth/session";
import type { MdDemoAtomDistanceRequest, MdDemoRunRequest, MdDemoRunResponse } from "../types";
import { useMdSimulationDemo } from "./useMdSimulationDemo";
const api = vi.hoisted(() => ({ defaults: vi.fn(), run: vi.fn(), distance: vi.fn() }));
vi.mock("../services/api", () => ({ fetchMdDemoDefaults: api.defaults, runMdDemo: api.run, calculateMdDemoAtomDistance: api.distance, isApiRequestError: () => false }));
const request = { smiles: "*CC*", temperature: 300, pressure: 1, n_atom: 1000, n_chain: 10, forcefield: "GAFF2_mod" } as MdDemoRunRequest;
const kept = { run_id: "synthetic-kept-result", input: request } as MdDemoRunResponse;
const tick = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); };
beforeEach(() => { vi.useFakeTimers(); vi.clearAllMocks(); api.defaults.mockResolvedValue({ default_request: request }); api.run.mockResolvedValue(kept); api.distance.mockResolvedValue({ stats: { n_frames: 1 } }); });
afterEach(() => { cleanup(); vi.useRealTimers(); });
describe("example MD visibility lifecycle", () => {
  it("does not issue any request when disabled, including explicit callbacks", async () => {
    const { result } = renderHook(() => useMdSimulationDemo({ enabled: false, resultRevealDelayMs: 0 }));
    await act(async () => { await result.current.refreshDefaults(); await result.current.run(request); await result.current.calculateDistance({ atom_id_1: 1, atom_id_2: 2, use_pbc: true }); });
    expect(api.defaults).not.toHaveBeenCalled(); expect(api.run).not.toHaveBeenCalled(); expect(api.distance).not.toHaveBeenCalled();
  });
  it("preserves completed data, stops interrupted loading, and fences old callbacks while hidden", async () => {
    const { result, rerender } = renderHook(({ enabled }) => useMdSimulationDemo({ enabled, resultRevealDelayMs: 0 }), { initialProps: { enabled: true } });
    await tick(); await act(async () => { await result.current.run(request); });
    expect(result.current.data?.run_id).toBe(kept.run_id);
    let resolve!: (value: MdDemoRunResponse) => void;
    const pending = new Promise<MdDemoRunResponse>(next => { resolve = next; });
    let signal!: AbortSignal;
    api.run.mockImplementationOnce((_request: MdDemoRunRequest, next: AbortSignal) => { signal = next; return pending; });
    let running!: Promise<MdDemoRunResponse | null>;
    act(() => { running = result.current.run(request); });
    const oldRun = result.current.run;
    rerender({ enabled: false });
    expect(signal.aborted).toBe(true);
    expect(result.current.data?.run_id).toBe(kept.run_id);
    expect(result.current.runLoading).toBe(false);
    const calls = api.run.mock.calls.length;
    await act(async () => { await oldRun(request); });
    expect(api.run).toHaveBeenCalledTimes(calls);
    await act(async () => { resolve({ ...kept, run_id: "late" }); await running; });
    expect(result.current.data?.run_id).toBe(kept.run_id);
    rerender({ enabled: true }); await tick();
    expect(result.current.data?.run_id).toBe(kept.run_id);
  });
});


it("rejects old defaults after pause and resume even when the mock ignores abort", async () => {
  let resolve!: (value: unknown) => void;
  const pending = new Promise(next => { resolve = next; });
  api.defaults.mockReturnValueOnce(pending).mockResolvedValue({ default_request: request, marker: "resumed" });
  const { result, rerender } = renderHook(({ enabled }) => useMdSimulationDemo({ enabled, resultRevealDelayMs: 0 }), { initialProps: { enabled: true } });
  await tick(); rerender({ enabled: false }); rerender({ enabled: true }); await tick();
  const resumed = result.current.defaults;
  await act(async () => { resolve({ default_request: { ...request, temperature: 999 } }); await pending; });
  expect(result.current.defaults).toBe(resumed);
});

it("stops distance loading and rejects a late distance after resuming", async () => {
  const { result, rerender } = renderHook(({ enabled }) => useMdSimulationDemo({ enabled, resultRevealDelayMs: 0 }), { initialProps: { enabled: true } });
  await tick(); await act(async () => { await result.current.run(request); });
  let resolve!: (value: unknown) => void;
  const pending = new Promise(next => { resolve = next; });
  api.distance.mockReturnValueOnce(pending);
  let oldAction!: ReturnType<typeof result.current.calculateDistance>;
  act(() => { oldAction = result.current.calculateDistance({ atom_id_1: 1, atom_id_2: 2, use_pbc: true }); });
  rerender({ enabled: false }); expect(result.current.distanceLoading).toBe(false);
  rerender({ enabled: true }); await tick();
  await act(async () => { await result.current.calculateDistance({ atom_id_1: 2, atom_id_2: 3, use_pbc: true }); });
  const resumed = result.current.distance;
  await act(async () => { resolve({ stats: { n_frames: 999 } }); await oldAction; });
  expect(result.current.distance).toBe(resumed); expect(result.current.data).toEqual(kept);
});

it("clears defaults, results and distances on identity retirement while paused", async () => {
  const { result, rerender } = renderHook(({ enabled }) => useMdSimulationDemo({ enabled, resultRevealDelayMs: 0 }), { initialProps: { enabled: true } });
  await tick(); await act(async () => { await result.current.run(request); await result.current.calculateDistance({ atom_id_1: 1, atom_id_2: 2, use_pbc: true }); });
  rerender({ enabled: false }); expect(result.current.data).toEqual(kept);
  act(() => retireSession());
  expect(result.current.data).toBeNull(); expect(result.current.defaults).toBeNull(); expect(result.current.distance).toBeNull();
  expect(result.current.runLoading).toBe(false); expect(result.current.distanceLoading).toBe(false);
  const calls = api.defaults.mock.calls.length;
  rerender({ enabled: true }); await tick(); expect(api.defaults).toHaveBeenCalledTimes(calls);
});
