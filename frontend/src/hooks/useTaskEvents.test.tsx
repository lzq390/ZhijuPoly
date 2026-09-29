// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installSession, retireSession } from "../auth/session";
import { useTaskEvents } from "./useTaskEvents";

let stream: ReadableStreamDefaultController<Uint8Array>;
let fetchMock: ReturnType<typeof vi.fn>;
const encoder = new TextEncoder();
beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(new ReadableStream({
    start(controller) { stream = controller; }
  }), { headers: { "Content-Type": "text/event-stream" } })));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
async function send(jobs: unknown[]) {
  await act(async () => { stream.enqueue(encoder.encode(`event: snapshot\ndata: ${JSON.stringify({ module: "md", jobs })}\n\n`)); });
  await act(async () => { await vi.advanceTimersByTimeAsync(101); });
}

it("does not refresh for unchanged state, heartbeat or 60 seconds of idle time", async () => {
  const changed = vi.fn();
  renderHook(() => useTaskEvents("md", true, changed)); await flush();
  const job = { job_id: "a", status: "running", queue_position: null, artifacts_deleted: false };
  await send([job]); changed.mockClear();
  await send([job]);
  await act(async () => { stream.enqueue(encoder.encode(": keepalive\n\n")); await vi.advanceTimersByTimeAsync(60000); });
  expect(changed).not.toHaveBeenCalled(); expect(fetchMock).toHaveBeenCalledTimes(1);
  await send([{ ...job, status: "completed" }]);
  expect(changed).toHaveBeenCalledExactlyOnceWith({ ids: ["a"], resync: false });
});

it("deduplicates notification transitions and deletions", async () => {
  const changed = vi.fn();
  renderHook(() => useTaskEvents("md", true, changed)); await flush();
  const job = { job_id: "a", status: "running" };
  await send([job]); changed.mockClear();
  await send([{ ...job, status: "cancelled" }]);
  expect(changed).toHaveBeenCalledExactlyOnceWith({ ids: ["a"], resync: false });
  changed.mockClear();
  await send([{ ...job, status: "cancelled" }]);
  expect(changed).not.toHaveBeenCalled();
  await send([]);
  expect(changed).toHaveBeenCalledExactlyOnceWith({ ids: ["a"], resync: false });
  changed.mockClear(); await send([]);
  expect(changed).not.toHaveBeenCalled();
});

it("resynchronizes unchanged snapshots once after unavailable recovers on the same stream", async () => {
  const changed = vi.fn();
  const { result } = renderHook(() => useTaskEvents("md", true, changed)); await flush();
  await send([]); changed.mockClear();
  await act(async () => {
    stream.enqueue(encoder.encode('event: unavailable\ndata: {"module":"md","unavailable":true}\n\n'));
  });
  expect(result.current.connectionState).toBe("unavailable");
  await send([]);
  expect(result.current.connectionState).toBe("live");
  expect(changed).toHaveBeenCalledExactlyOnceWith({ ids: [], resync: true });
  changed.mockClear(); await send([]);
  expect(changed).not.toHaveBeenCalled();
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("resynchronizes after disconnection without starting a list polling timer", async () => {
  const changed = vi.fn();
  renderHook(() => useTaskEvents("md", true, changed)); await flush();
  await send([]); changed.mockClear();
  await act(async () => { stream.close(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1001); });
  expect(fetchMock).toHaveBeenCalledTimes(2);
  await send([{ job_id: "new", status: "completed" }]);
  expect(changed).toHaveBeenCalledExactlyOnceWith({ ids: ["new"], resync: true });
});

it("retires the old subscription and rejects old-user deliveries after switching", async () => {
  const changed = vi.fn();
  renderHook(() => useTaskEvents("md", true, changed)); await flush();
  await send([]); changed.mockClear();
  await act(async () => {
    stream.enqueue(encoder.encode('event: snapshot\ndata: {"module":"md","jobs":[{"job_id":"old","status":"completed"}]}\n\n'));
    retireSession();
    installSession({ authenticated: true, user: { id: "B", username: "B", must_change_password: false }, session_id: "B", csrf_token: "B", capabilities: {} });
    await vi.advanceTimersByTimeAsync(60000);
  });
  expect(changed).not.toHaveBeenCalled(); expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("stops retries on an older backend without an event endpoint", async () => {
  fetchMock.mockResolvedValue(new Response('{}', { status: 404 }));
  const changed = vi.fn();
  const { result } = renderHook(() => useTaskEvents("md", true, changed)); await flush();
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(result.current.connectionState).toBe("unavailable");
  expect(fetchMock).toHaveBeenCalledTimes(1); expect(changed).not.toHaveBeenCalled();
});
