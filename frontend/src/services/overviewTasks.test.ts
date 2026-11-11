import { afterEach, describe, expect, it, vi } from "vitest";
import { retireSession } from "../auth/session";
import type { OverviewTask } from "../types/overview";
import { fetchOverviewTasks } from "./overviewTasks";

type Source = OverviewTask["source"];
const paths: Record<Source, string> = {
  monomerMd: "/api/v1/monomer-md/jobs", monomerDft: "/api/v1/monomer-dft/jobs",
  polymerMd: "/api/v1/polymer-md/jobs", polymerizationBatch: "/api/v1/monomer-polymerization/batch/jobs"
};
type Row = Record<string, unknown>;
function task(source: Source, id = "same-job", extra: Row = {}): Row {
  const common = { job_id: id, status: "running", created_at: "2026-11-05T08:30:00+08:00", updated_at: "2026-11-05T01:00:00Z", finished_at: null };
  const fields: Record<Source, Row> = {
    monomerMd: { run_mode: "formal", protocol: "Density", progress_percent: 25, progress_stage: "running" },
    monomerDft: { calculation_type: "optimization", progress_percent: 37.5, stage: "optimization" },
    polymerMd: { stage: "eq3", quality_status: "not_evaluated", scientific_request: { calculation: "basic_tc" } },
    polymerizationBatch: { target_class: "polyimide", stage: "export", summary: { processed_pairs: 100, valid_pairs: 100 } }
  };
  return { ...common, ...fields[source], ...extra };
}
function page(source: Source, items: Row[] = [], extra: Row = {}): Row {
  return source === "polymerizationBatch" ? { items, total: items.length, next_offset: null, ...extra }
    : { items, total: items.length, page: 1, page_size: 100, ...extra };
}
function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
function transport(handler: (source: Source, url: URL) => unknown | Promise<unknown>) {
  const fetch = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = new URL(String(input), "http://test.local");
    const source = (Object.keys(paths) as Source[]).find(key => paths[key] === url.pathname);
    if (!source) throw new Error("Unexpected endpoint");
    const body = await handler(source, url);
    return body instanceof Response ? body : json(body);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}
afterEach(() => vi.unstubAllGlobals());

describe("personal overview task aggregation", () => {
  it("reads only the four personal list endpoints with authenticated GETs and excludes MD demo requests", async () => {
    const fetch = transport(source => page(source, [task(source)]));
    const sources = await fetchOverviewTasks();
    expect(sources.map(source => [source.id, source.status, source.total, source.tasks.length])).toEqual([
      ["monomerMd", "ready", 1, 1], ["monomerDft", "ready", 1, 1], ["polymerMd", "ready", 1, 1], ["polymerizationBatch", "ready", 1, 1]
    ]);
    expect(new Set(sources.flatMap(source => source.tasks.map(task => task.id))).size).toBe(4);
    expect(sources.map(source => source.tasks[0].module)).toEqual(["monomerMdSimulation", "monomerDft", "mdSimulationDemo", "monomerPolymerization"]);
    expect(sources[0].tasks[0]).toMatchObject({ title: "密度计算 · same-job", progress: 25, outcome: "active", createdAt: "2026-11-05T00:30:00.000Z" });
    expect(sources[1].tasks[0]).toMatchObject({ title: "结构优化 · same-job", progress: 37.5, stage: "结构优化" });
    expect(sources[2].tasks[0]).toMatchObject({ title: "聚合物 导热计算 · same-job", progress: null, stage: "EQ3 平衡采样" });
    expect(sources[3].tasks[0]).toMatchObject({ title: "聚酰亚胺聚合 · same-job", progress: null, stage: "整理输出文件" });
    for (const [input, init] of fetch.mock.calls) {
      const url = new URL(String(input), "http://test.local");
      expect(init?.method ?? "GET").toBe("GET");
      expect(init?.body).toBeUndefined();
      expect(new Headers(init?.headers).get("X-Session-Context")).toBe("test-session");
      expect(new Headers(init?.headers).has("X-CSRF-Token")).toBe(false);
      expect(url.searchParams.get(url.pathname.includes("/batch/") ? "limit" : "page_size")).toBe("100");
      expect(init?.cache).toBe("no-store");
    }
    const md = new URL(String(fetch.mock.calls.find(([input]) => String(input).includes("monomer-md"))?.[0]), "http://test.local");
    expect(md.searchParams.get("run_mode")).toBe("formal");
    expect(md.searchParams.get("include_result")).toBe("false");
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it("starts all four independent sources concurrently", async () => {
    const pending: Array<() => void> = [];
    transport(source => new Promise(resolve => pending.push(() => resolve(page(source)))));
    const loading = fetchOverviewTasks();
    expect(pending).toHaveLength(4);
    pending.forEach(resolve => resolve());
    expect((await loading).every(source => source.status === "ready" && source.total === 0)).toBe(true);
  });

  it("follows both page numbers and batch offsets until every record has been read", async () => {
    const fetch = transport((source, url) => {
      if (source === "monomerMd") {
        const index = Number(url.searchParams.get("page"));
        return page(source, [task(source, `md-${index}`)], { total: 2, page: index, page_size: 1 });
      }
      if (source === "polymerizationBatch") {
        const offset = Number(url.searchParams.get("offset"));
        return page(source, [task(source, `batch-${offset}`)], { total: 2, next_offset: offset === 0 ? 1 : null });
      }
      return page(source);
    });
    const sources = await fetchOverviewTasks();
    expect(sources[0]).toMatchObject({ status: "ready", total: 2 });
    expect(sources[0].tasks.map(task => task.jobId)).toEqual(["md-1", "md-2"]);
    expect(sources[3]).toMatchObject({ status: "ready", total: 2 });
    expect(sources[3].tasks.map(task => task.jobId)).toEqual(["batch-0", "batch-1"]);
    expect(fetch).toHaveBeenCalledTimes(6);
  });

  it("retains completed pages and the reported total when a later page fails, without hiding other sources", async () => {
    transport((source, url) => {
      if (source !== "monomerMd") return page(source, [task(source)]);
      return url.searchParams.get("page") === "1" ? page(source, [task(source, "first")], { total: 2, page_size: 1 }) : json({ detail: "private backend error" }, 503);
    });
    const sources = await fetchOverviewTasks();
    expect(sources[0]).toMatchObject({ status: "partial", total: 2 });
    expect(sources[0].tasks.map(task => task.jobId)).toEqual(["first"]);
    expect(sources[0].message).not.toContain("private backend error");
    expect(sources.slice(1).every(source => source.status === "ready")).toBe(true);
  });

  it("marks first-page failure unavailable with an unknown total, preserving successful empty sources", async () => {
    transport(source => source === "monomerMd" ? json({ detail: "not ready" }, 503) : page(source));
    const sources = await fetchOverviewTasks();
    expect(sources[0]).toMatchObject({ status: "unavailable", total: null, tasks: [] });
    expect(sources.slice(1).every(source => source.status === "ready" && source.total === 0)).toBe(true);
  });

  it("removes demo records from a bad formal response and marks unknown states as partial", async () => {
    transport(source => source === "monomerMd" ? page(source, [
      task(source, "demo", { run_mode: "demo", protocol: "DensityDemo" }),
      task(source, "missing-mode", { run_mode: null }),
      task(source, "formal", { status: "unrecognized_state" })
    ]) : page(source));
    const sources = await fetchOverviewTasks();
    expect(sources[0]).toMatchObject({ status: "partial", total: 3 });
    expect(sources[0].tasks).toHaveLength(1);
    expect(sources[0].tasks[0]).toMatchObject({ jobId: "formal", outcome: "unknown", statusLabel: "状态待确认" });
  });

  it("does not turn cancellation intent, failed work or completed work with errors into success", async () => {
    transport(source => page(source, [task(source, "a", { status: source === "polymerizationBatch" ? "cancelling" : "cancel_requested" }),
      task(source, "b", { status: "failed" }), task(source, "c", { status: "cancelled" }),
      task(source, "d", { status: source === "polymerizationBatch" ? "completed_with_errors" : "completed", quality_status: "not_converged" })]));
    const sources = await fetchOverviewTasks();
    for (const source of sources) expect(source.tasks.slice(0, 3).map(task => task.outcome)).toEqual(["active", "attention", "cancelled"]);
    expect(sources[0].tasks[3].outcome).toBe("completed");
    expect(sources[1].tasks[3].outcome).toBe("completed");
    expect(sources[2].tasks[3]).toMatchObject({ outcome: "attention", statusLabel: "已完成，质量需关注" });
    expect(sources[3].tasks[3].outcome).toBe("attention");
  });

  it.each([-1, 101, "50", null])("does not invent or clamp invalid percentages (%s)", async progress => {
    transport(source => page(source, [task(source, "job", { progress_percent: progress })]));
    const sources = await fetchOverviewTasks();
    expect(sources.every(source => source.tasks[0].progress === null)).toBe(true);
    if (progress !== null) expect(sources.slice(0, 2).every(source => source.status === "partial")).toBe(true);
  });

  it("keeps honest 0/100 percentages and leaves polymer MD and batch global progress unknown", async () => {
    transport(source => page(source, [task(source, "zero", { progress_percent: 0 }), task(source, "full", { progress_percent: 100 })]));
    const sources = await fetchOverviewTasks();
    expect(sources.slice(0, 2).map(source => source.tasks.map(task => task.progress))).toEqual([[0, 100], [0, 100]]);
    expect(sources.slice(2).map(source => source.tasks.map(task => task.progress))).toEqual([[null, null], [null, null]]);
  });

  it("keeps unavailable dates null instead of substituting the current date", async () => {
    transport(source => page(source, [task(source, "undated", { created_at: "not-a-date", updated_at: "2026-11-05T00:00:00", finished_at: "invalid" })]));
    const sources = await fetchOverviewTasks();
    for (const source of sources) {
      expect(source.status).toBe("partial");
      expect(source.tasks[0]).toMatchObject({ createdAt: null, updatedAt: null, finishedAt: null });
    }
  });

  it.each([
    "/srv/private/jobs/result.log",
    "C:\\private\\worker\\result.log",
    "RuntimeError: failed with private worker context",
    "Traceback (most recent call last)",
    "unrecognized_worker_stage",
    "constructor",
    "__proto__",
    `additional_${"1".repeat(50)}`
  ])("does not publish unknown stages or worker error/path text (%s)", async stage => {
    transport(source => page(source, [task(source, "job", { progress_stage: stage, stage })]));
    const sources = await fetchOverviewTasks();
    expect(sources.every(source => source.tasks[0].stage === null)).toBe(true);
    expect(sources.every(source => source.status === "ready")).toBe(true);
    expect(JSON.stringify(sources)).not.toContain(stage);
  });

  it("keeps known stage labels and bounded additional-equilibration ordinals", async () => {
    transport(source => page(source, [task(source, "a", { progress_stage: "conformer", stage: "additional_2" }),
      task(source, "b", { progress_stage: "optimization", stage: "analyze_0" })]));
    const sources = await fetchOverviewTasks();
    expect(sources[0].tasks.map(task => task.stage)).toEqual(["构象生成", "结构优化"]);
    expect(sources.slice(1).every(source => source.tasks[0].stage === "第 2 次追加平衡" && source.tasks[1].stage === "基础物性分析")).toBe(true);
  });

  it.each(["unknown_metadata", "constructor", "__proto__", "toString"])("uses plain title fallbacks for unknown or inherited metadata (%s)", async value => {
    transport(source => page(source, [task(source, "job", { protocol: value, target_class: value })]));
    const sources = await fetchOverviewTasks();
    expect(sources[0].tasks[0].title).toBe("单体 MD · job");
    expect(sources[3].tasks[0].title).toBe("批量聚合 · job");
    expect(JSON.stringify(sources)).not.toContain("[native code]");
  });

  it("deduplicates overlapping pages, preserves newly observed rows, and stops safely", async () => {
    const fetch = transport((source, url) => source === "monomerDft" ? page(source,
      [task(source, "a"), task(source, url.searchParams.get("page") === "1" ? "b" : "c")],
      { total: 5, page: Number(url.searchParams.get("page")), page_size: 2 }) : page(source));
    const sources = await fetchOverviewTasks();
    expect(sources[1]).toMatchObject({ status: "partial", total: 5 });
    expect(sources[1].tasks.map(task => task.jobId)).toEqual(["a", "b", "c"]);
    expect(fetch.mock.calls.filter(([input]) => String(input).includes("monomer-dft"))).toHaveLength(2);
  });

  it.each([0, -1, undefined, 2])("stops a malformed/nonadvancing batch cursor (%s)", async next => {
    const fetch = transport(source => source === "polymerizationBatch" ? page(source, [task(source)], { total: 3, next_offset: next }) : page(source));
    const sources = await fetchOverviewTasks();
    expect(sources[3]).toMatchObject({ status: "partial", total: 3 });
    expect(sources[3].tasks).toHaveLength(1);
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it("marks prematurely exhausted pagination partial rather than assuming an empty remainder", async () => {
    transport(source => page(source, [task(source)], { total: 3 }));
    const sources = await fetchOverviewTasks();
    expect(sources.every(source => source.status === "partial" && source.total === 3 && source.tasks.length === 1)).toBe(true);
  });

  it("detects page numbers that stop advancing", async () => {
    transport((source, url) => source === "polymerMd" ? page(source, [task(source, `job-${url.searchParams.get("page")}`)], { total: 3, page: 1, page_size: 1 }) : page(source));
    const source = (await fetchOverviewTasks())[2];
    expect(source).toMatchObject({ status: "partial", total: 3 });
    expect(source.tasks.map(task => task.jobId)).toEqual(["job-1", "job-2"]);
  });

  it("bounds per-source records and requests without presenting a capped read as complete", async () => {
    const fetch = transport((source, url) => {
      if (source !== "monomerDft") return page(source);
      const index = Number(url.searchParams.get("page"));
      return page(source, Array.from({ length: 100 }, (_, i) => task(source, `job-${index}-${i}`)), { page: index, total: 20_000 });
    });
    const source = (await fetchOverviewTasks())[1];
    expect(source).toMatchObject({ status: "partial", total: 20_000 });
    expect(source.tasks).toHaveLength(10_000);
    expect(fetch.mock.calls.filter(([input]) => String(input).includes("monomer-dft"))).toHaveLength(100);
  });

  it.each(["monomerDft", "polymerizationBatch"] as const)("accepts an exact 10,000-record terminal page for %s", async target => {
    const fetch = transport((source, url) => {
      if (source !== target) return page(source);
      const pageNumber = Number(url.searchParams.get("page") ?? 1);
      const offset = source === "polymerizationBatch" ? Number(url.searchParams.get("offset")) : (pageNumber - 1) * 100;
      return page(source, Array.from({ length: 100 }, (_, i) => task(source, `job-${offset + i}`)), {
        total: 10_000, page: pageNumber, next_offset: offset + 100 < 10_000 ? offset + 100 : null,
      });
    });
    const source = (await fetchOverviewTasks()).find(source => source.id === target)!;
    expect(source).toMatchObject({ status: "ready", total: 10_000 });
    expect(source.tasks).toHaveLength(10_000);
    expect(source.message).toBeUndefined();
    expect(fetch.mock.calls.filter(([input]) => String(input).split("?")[0] === paths[target])).toHaveLength(100);
  });

  it.each(["monomerDft", "polymerizationBatch"] as const)("keeps invalid terminal pagination partial at the 10,000-record boundary for %s", async target => {
    transport((source, url) => {
      if (source !== target) return page(source);
      const pageNumber = Number(url.searchParams.get("page") ?? 1);
      const offset = source === "polymerizationBatch" ? Number(url.searchParams.get("offset")) : (pageNumber - 1) * 100;
      return page(source, Array.from({ length: 100 }, (_, i) => task(source, `job-${offset + i}`)), {
        total: 10_000,
        page: pageNumber === 100 ? 99 : pageNumber,
        next_offset: offset + 100,
      });
    });
    const source = (await fetchOverviewTasks()).find(source => source.id === target)!;
    expect(source).toMatchObject({ status: "partial", total: 10_000 });
    expect(source.tasks).toHaveLength(10_000);
    expect(source.message).toBeTruthy();
  });

  it("keeps missing totals unknown while retaining any valid rows", async () => {
    transport(source => page(source, [task(source)], { total: undefined }));
    const sources = await fetchOverviewTasks();
    expect(sources.every(source => source.status === "partial" && source.total === null && source.tasks.length === 1)).toBe(true);
  });

  it("does not absorb AbortError as an unavailable source", async () => {
    transport(source => {
      if (source === "monomerMd") throw new DOMException("Cancelled", "AbortError");
      return page(source);
    });
    await expect(fetchOverviewTasks()).rejects.toMatchObject({ name: "AbortError" });
  });

  it("makes no requests when the caller has already cancelled", async () => {
    const fetch = transport(source => page(source));
    const controller = new AbortController(); controller.abort();
    await expect(fetchOverviewTasks(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each(["abort", "session"])("discards delayed source data after %s retirement", async reason => {
    let release!: (value: unknown) => void;
    transport(source => source === "monomerMd" ? new Promise(resolve => { release = resolve; }) : page(source));
    const controller = new AbortController();
    const loading = fetchOverviewTasks(controller.signal);
    const rejected = expect(loading).rejects.toMatchObject({ name: "AbortError" });
    if (reason === "abort") controller.abort(); else retireSession();
    release(page("monomerMd", [task("monomerMd")]));
    await rejected;
  });
});
