import { afterEach, expect, it, vi } from "vitest";
import { gpuActivityItem, gpuHistoricalTask, gpuTaskHistory } from "../test/gpuTaskHistoryFixture";
import { fetchGpuTaskHistory } from "./gpuTaskHistory";
import { MonitoringRequestError } from "./serverMonitoring";

afterEach(() => { vi.unstubAllGlobals(); });
it("reads verified all-GPU history using the private session context", async () => {
  const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(gpuTaskHistory()), { status: 200 })); vi.stubGlobal("fetch", fetcher);
  expect((await fetchGpuTaskHistory(new AbortController().signal)).coverage).toBe("retained_verified_gpu_records");
  expect(String(fetcher.mock.calls[0][0])).toContain("/server-monitoring/gpu-tasks");
  expect(new Headers((fetcher.mock.calls[0][1] as RequestInit).headers).get("X-Session-Context")).toBe("test-session");
});
it.each(["unknown-gpu", "running", "invalid-time", "negative-duration", "wrong-coverage"])("rejects malformed or unverified task data: %s", invalid => {
  const value = gpuTaskHistory() as unknown as { coverage: string; tasks: Array<Record<string, unknown>> };
  if (invalid === "unknown-gpu") value.tasks[0].gpu_uuid = "unverified";
  if (invalid === "running") value.tasks[0].status = "running";
  if (invalid === "invalid-time") value.tasks[0].finished_at = "invalid";
  if (invalid === "negative-duration") value.tasks[0].duration_seconds = -1;
  if (invalid === "wrong-coverage") value.coverage = "all_tasks";
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value), { status: 200 })));
  return expect(fetchGpuTaskHistory(new AbortController().signal)).rejects.toBeInstanceOf(MonitoringRequestError);
});

it("accepts verified current activity plus formal, demo and polymer MD records", async () => {
  const value = gpuTaskHistory({ tasks: [
    gpuHistoricalTask({ module: "monomer_md", calculation_type: "md_formal" }),
    gpuHistoricalTask({ id: "bbbbbbbbbbbbbbbbbbbb", module: "monomer_md", calculation_type: "md_demo" }),
    gpuHistoricalTask({ id: "cccccccccccccccccccc", module: "polymer_md", calculation_type: "polymer_md" })
  ] });
  value.activity.items = [gpuActivityItem()];
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value), { status: 200 })));
  const result = await fetchGpuTaskHistory(new AbortController().signal);
  expect(result.sources).toHaveLength(6); expect(result.activity.items[0].kind).toBe("service"); expect(result.tasks).toHaveLength(3);
});

it.each(["unverified-gpu", "false-task", "unknown-kind", "negative-memory", "invalid-start", "unsafe-label", "invalid-history-clock", "unknown-source", "wrong-module-type"])("rejects malformed current activity or extended history: %s", invalid => {
  const value = gpuTaskHistory() as unknown as { history_captured_at: string; activity: { items: Array<Record<string, unknown>> }; sources: Array<Record<string, unknown>>; tasks: Array<Record<string, unknown>> };
  value.activity.items = [gpuActivityItem()];
  if (invalid === "unverified-gpu") value.activity.items[0].gpu_uuid = "unverified";
  if (invalid === "false-task") value.activity.items[0].status = "running";
  if (invalid === "unknown-kind") value.activity.items[0].kind = "__proto__";
  if (invalid === "negative-memory") value.activity.items[0].memory_used_bytes = -1;
  if (invalid === "invalid-start") value.activity.items[0].started_at = "bad";
  if (invalid === "unsafe-label") value.activity.items[0].label = "process\nsecret";
  if (invalid === "invalid-history-clock") value.history_captured_at = "invalid";
  if (invalid === "unknown-source") value.sources[0].source_id = "unknown";
  if (invalid === "wrong-module-type") value.tasks[0].calculation_type = "md_formal";
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(value), { status: 200 })));
  return expect(fetchGpuTaskHistory(new AbortController().signal)).rejects.toBeInstanceOf(MonitoringRequestError);
});
