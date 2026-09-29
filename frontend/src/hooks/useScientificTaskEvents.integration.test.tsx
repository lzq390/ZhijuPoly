// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useMonomerMdSimulation } from "./useMonomerMdSimulation";
import { useMonomerDftJob } from "./useMonomerDftJob";

const ID = "188817d4-bdd2-4e25-9336-bfcbccc16a61";
const encoder = new TextEncoder();
let channel: ReadableStreamDefaultController<Uint8Array>;
let paths: string[];
let job: Record<string, unknown>;
let cancelReply: ((response: Response) => void) | null;
let failJobReads: boolean;
beforeEach(() => {
  vi.useFakeTimers(); paths = []; cancelReply = null; failJobReads = false;
  job = { job_id: ID, status: "running", run_mode: "formal", protocol: "Density", queue_position: null,
    attempt: 1, artifacts_state: "none", request: { model: "aimnet2" }, error: null, artifacts: [] };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); paths.push(`${init?.method ?? "GET"} ${url}`);
    if (url.includes("/task-events")) return new Response(new ReadableStream({ start(c) { channel = c; } }), { headers: { "Content-Type": "text/event-stream" } });
    if (url.endsWith("/cancel")) return new Promise<Response>(resolve => { cancelReply = resolve; });
    if (failJobReads && url.includes("/jobs")) return Response.json({ detail: "temporary storage failure" }, { status: 503 });
    let data: unknown;
    if (url.includes("/jobs?")) data = { items: [job], page: 1, page_size: 10, total: 1 };
    else if (url.endsWith(`/jobs/${ID}`)) data = job;
    else if (url.endsWith("/capabilities")) data = { enabled: true, available: true, schema_ready: true, models: [] };
    else if (url.endsWith("/protocols")) data = { enabled: true, available: true, protocols: [] };
    else if (url.endsWith("/status")) data = { enabled: true, available: true, schema_ready: true, busy: true };
    else throw new Error(`Unexpected test request: ${url}`);
    return Response.json(data);
  }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
async function flush() { await act(async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); }); }
async function snapshot(module: "md" | "dft") {
  const summary = module === "md"
    ? { job_id: ID, status: job.status, queue_position: null, artifacts_deleted: false }
    : { job_id: ID, status: job.status, queue_position: null, attempt: 1, artifacts_state: job.artifacts_state };
  await act(async () => { channel.enqueue(encoder.encode(`event: snapshot\ndata: ${JSON.stringify({ module, jobs: [summary] })}\n\n`)); });
  await act(async () => { await vi.advanceTimersByTimeAsync(101); }); await flush();
}

it.each(["md", "dft"] as const)("%s has no periodic requests and refreshes once on a completed event", async module => {
  const { result } = renderHook(() => module === "md"
    ? useMonomerMdSimulation({ enabled: true, initialJobId: ID, taskCenterActive: true })
    : useMonomerDftJob({ enabled: true, initialJobId: ID }));
  await flush(); await snapshot(module);
  const baseline = paths.length;
  await snapshot(module);
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(paths).toHaveLength(baseline);
  job = { ...job, status: "completed" };
  await snapshot(module);
  expect(result.current.job?.status).toBe("completed");
  expect(result.current.isJobLoading).toBe(false);
  expect(paths.slice(baseline).filter(p => p.endsWith(`/jobs/${ID}`))).toHaveLength(1);
  const afterEvent = paths.length;
  await snapshot(module);
  expect(paths).toHaveLength(afterEvent);
  await act(async () => { await result.current.refreshAll(); });
  expect(paths.filter(p => p.endsWith(`/jobs/${ID}`)).length).toBeGreaterThan(1);
});

it.each(["md", "dft"] as const)("%s updates selected detail when history reads completion before the event", async module => {
  const { result } = renderHook(() => module === "md"
    ? useMonomerMdSimulation({ enabled: true, initialJobId: ID, taskCenterActive: true })
    : useMonomerDftJob({ enabled: true, initialJobId: ID }));
  await flush(); await snapshot(module);
  expect(result.current.job?.status).toBe("running");
  job = { ...job, status: "completed" };
  await act(async () => { await result.current.refreshHistory(); });
  expect(result.current.history?.items[0].status).toBe("completed");
  expect(result.current.job?.status).toBe("running");
  const readsBeforeEvent = paths.filter(p => p.endsWith(`/jobs/${ID}`)).length;
  await snapshot(module);
  expect(result.current.job?.status).toBe("completed");
  expect(paths.filter(p => p.endsWith(`/jobs/${ID}`))).toHaveLength(readsBeforeEvent + 1);
  const afterEvent = paths.length;
  await snapshot(module);
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(paths).toHaveLength(afterEvent);
});

it.each(["md", "dft"] as const)("%s updates history when selected detail reads completion before the event", async module => {
  const { result } = renderHook(() => module === "md"
    ? useMonomerMdSimulation({ enabled: true, initialJobId: ID, taskCenterActive: true })
    : useMonomerDftJob({ enabled: true, initialJobId: ID }));
  await flush(); await snapshot(module);
  job = { ...job, status: "completed" };
  await act(async () => { await result.current.loadJob(ID); }); await flush();
  expect(result.current.job?.status).toBe("completed");
  expect(result.current.history?.items[0].status).toBe("running");
  await snapshot(module);
  expect(result.current.history?.items[0].status).toBe("completed");
  const afterEvent = paths.length;
  await snapshot(module);
  expect(paths).toHaveLength(afterEvent);
});

it.each(["md", "dft"] as const)("%s retries failed event reads once after notification availability recovers", async module => {
  const { result } = renderHook(() => module === "md"
    ? useMonomerMdSimulation({ enabled: true, initialJobId: ID, taskCenterActive: true })
    : useMonomerDftJob({ enabled: true, initialJobId: ID }));
  await flush(); await snapshot(module);
  failJobReads = true;
  job = { ...job, status: "completed" };
  await snapshot(module);
  expect(result.current.job?.status).toBe("running");
  await act(async () => {
    channel.enqueue(encoder.encode(`event: unavailable\ndata: ${JSON.stringify({ module, unavailable: true })}\n\n`));
  });
  expect(result.current.eventConnectionState).toBe("unavailable");
  failJobReads = false;
  const readsBeforeRecovery = paths.filter(p => p.endsWith(`/jobs/${ID}`)).length;
  await snapshot(module);
  expect(result.current.eventConnectionState).toBe("live");
  expect(result.current.job?.status).toBe("completed");
  expect(result.current.history?.items[0].status).toBe("completed");
  expect(paths.filter(p => p.endsWith(`/jobs/${ID}`))).toHaveLength(readsBeforeRecovery + 1);
  const afterRecovery = paths.length;
  await snapshot(module);
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(paths).toHaveLength(afterRecovery);
});

it("DFT delivers a cancellation event that arrives before the cancel response", async () => {
  const { result } = renderHook(() => useMonomerDftJob({ initialJobId: ID }));
  await flush(); await snapshot("dft");
  let cancellation!: Promise<void>;
  act(() => { cancellation = result.current.cancel(); }); await flush();
  const intermediate = { ...job, status: "cancel_requested" };
  job = { ...job, status: "cancelled" };
  await snapshot("dft");
  await act(async () => { cancelReply!(Response.json(intermediate)); await cancellation; }); await flush();
  expect(result.current.job?.status).toBe("cancelled");
  expect(result.current.isJobLoading).toBe(false);
  expect(paths.filter(p => p.startsWith("POST"))).toHaveLength(1);
});

it("DFT observes artifact completion even when the computation was already terminal", async () => {
  job = { ...job, status: "completed", artifacts_state: "delete_requested" };
  const { result } = renderHook(() => useMonomerDftJob({ initialJobId: ID }));
  await flush(); await snapshot("dft");
  job = { ...job, artifacts_state: "deleted" };
  await snapshot("dft");
  expect(result.current.job?.artifacts_state).toBe("deleted");
  expect(result.current.pollState).toBe("terminal");
});

it("DFT retains capabilities when the first notification precedes initial loading", async () => {
  const baseFetch = globalThis.fetch;
  let initialCapabilitiesSignal: AbortSignal | undefined;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith("/capabilities") && !initialCapabilitiesSignal) {
      initialCapabilitiesSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        initialCapabilitiesSignal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        }, { once: true });
      });
    }
    return baseFetch(input, init);
  }));
  const { result } = renderHook(() => useMonomerDftJob({ initialJobId: ID }));
  await flush();
  expect(result.current.capabilities).toBeNull();
  await snapshot("dft");
  expect(initialCapabilitiesSignal?.aborted).toBe(true);
  expect(result.current.capabilities?.schema_ready).toBe(true);
  expect(result.current.job?.job_id).toBe(ID);
  expect(result.current.isServiceLoading).toBe(false);
});
