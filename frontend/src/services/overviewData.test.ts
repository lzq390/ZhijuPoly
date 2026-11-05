import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installSession, retireSession } from "../auth/session";
import type { KnowledgeSourcesResponse } from "../types";
import type { OverviewTaskSource } from "../types/overview";
import type { StructureRecordSummary } from "../structure-library/types";
import type { UsageActivity, UsageSummary } from "../types/usage";
import { fetchOverviewData } from "./overviewData";

const api = vi.hoisted(() => ({
  list: vi.fn(), tasks: vi.fn(), knowledge: vi.fn(), summary: vi.fn(), activity: vi.fn()
}));
vi.mock("../structure-library/api", () => ({ structureLibraryApi: { list: api.list } }));
vi.mock("./overviewTasks", () => ({ fetchOverviewTasks: api.tasks }));
vi.mock("./api", () => ({ fetchKnowledgeSources: api.knowledge }));
vi.mock("./usage", () => ({ fetchUsageSummary: api.summary, fetchUsageActivityByGroup: api.activity }));

const timestamp = "2026-10-21T03:45:00.000Z";
function member(id = "overview-a") {
  installSession({ authenticated: true, account_type: "member", user: { id, username: id, must_change_password: false },
    session_id: `session-${id}`, csrf_token: "fixture-csrf", capabilities: {} });
}
function structure(id: string): StructureRecordSummary {
  return { id, name: `结构 ${id}`, smiles: "CC", canonical_smiles: "CC", snapshot_schema_version: 1,
    editor_version: "3.8", preview_svg: null, metadata_revision: 1, created_at: timestamp, updated_at: timestamp };
}
function knowledge(sources?: KnowledgeSourcesResponse["sources"]): KnowledgeSourcesResponse {
  return { sources: sources ?? [
    { data_source: "legacy", enabled: true, ready: true, total: 12 },
    { data_source: "extracted_literature", enabled: true, ready: true, total: 8 }
  ] };
}
function summary(): UsageSummary {
  return { period: { start: timestamp, end: timestamp, timezone: "Asia/Shanghai", basis: "task_submitted_at" },
    totals: { tasks: 3, attempts: 3, child_calls: 0, states: { completed: 3 }, metrics: {} }, modules: [],
    daily: [{ date: "2026-10-21", tasks: 3, states: { completed: 3 } }],
    coverage: { uncovered: [], warnings: ["部分历史任务未纳入统计"] } };
}
function activity(): UsageActivity {
  return { mode: "activity_only", offset: 0, limit: 25, has_more: false, modules: [], module_groups: [], warnings: [],
    items: [{ module: "online_knowledge", task_id: "knowledge-job", attempt_id: null, kind: "task", status: "completed",
      submitted_at: timestamp, started_at: timestamp, finished_at: timestamp, parent_task_id: null,
      root_task_id: null, source: "native", metrics: {}, attempt_count: 1, updated_at: timestamp }] };
}
const taskSources: OverviewTaskSource[] = [{ id: "monomerMd", label: "单体 MD", status: "ready", total: 0, tasks: [] }];
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(finish => { resolve = finish; });
  return { promise, resolve };
}

beforeEach(() => {
  retireSession(); member(); vi.clearAllMocks();
  api.list.mockResolvedValue({ items: [], next_cursor: null });
  api.tasks.mockResolvedValue(taskSources);
  api.knowledge.mockResolvedValue(knowledge());
  api.summary.mockResolvedValue(summary());
  api.activity.mockResolvedValue(activity());
});
afterEach(() => { retireSession(); vi.useRealTimers(); });

describe("overview data sources", () => {
  it("reads every saved-structure page and keeps source totals separate", async () => {
    api.list.mockResolvedValueOnce({ items: [structure("first")], next_cursor: "page-2" })
      .mockResolvedValueOnce({ items: [structure("second")], next_cursor: "page-3" })
      .mockResolvedValueOnce({ items: [structure("third")], next_cursor: null });
    const controller = new AbortController();
    const result = await fetchOverviewData(controller.signal);
    expect(result.structures.status).toBe("ready");
    expect(result.structures.data).toEqual(["first", "second", "third"].map(id => ({ id, name: `结构 ${id}`, createdAt: timestamp, updatedAt: timestamp })));
    expect(api.list.mock.calls.map(call => call[1] ?? null)).toEqual([null, "page-2", "page-3"]);
    expect(api.list.mock.calls.every(call => call[0] === "" && call[2] instanceof AbortSignal && call[3] === 100)).toBe(true);
    expect(result.taskSources).toEqual(taskSources);
    expect(result.knowledge.status).toBe("ready");
    expect(result.knowledge.data?.sources.map(source => source.total)).toEqual([12, 8]);
    expect(result.knowledgeActivity.status).toBe("ready");
    expect(result.knowledgeActivity.data).toMatchObject({ dailyAvailable: true, recentAvailable: true });
    expect(result.knowledgeActivity.data?.daily).toEqual([{ date: "2026-10-21", count: 3 }]);
  });

  it.each(["later-page failure", "repeated cursor"])("retains saved structures as partial after %s", async failure => {
    api.list.mockResolvedValueOnce({ items: [structure("first")], next_cursor: "same-page" });
    if (failure === "later-page failure") api.list.mockRejectedValueOnce(new Error("结构库暂不可用"));
    else api.list.mockResolvedValue({ items: [structure("second")], next_cursor: "same-page" });
    const result = await fetchOverviewData();
    expect(result.structures.status).toBe("partial");
    expect(result.structures.data?.map(item => item.id)).toContain("first");
    expect(result.structures.message).toBeTruthy();
    expect(api.list.mock.calls.length).toBeLessThanOrEqual(3);
    expect(result.knowledge.status).toBe("ready");
  });

  it("marks a failed first structure page unavailable instead of reporting zero records", async () => {
    api.list.mockRejectedValue(new Error("结构库尚未启用"));
    const result = await fetchOverviewData();
    expect(result.structures).toMatchObject({ status: "unavailable", data: null });
    expect(result.taskSources).toEqual(taskSources);
    expect(result.knowledgeActivity.status).toBe("ready");
  });

  it("marks a bounded structure scan partial when more than 10,000 records exist", async () => {
    api.list.mockImplementation((_query: string, cursor?: string | null) => {
      const offset = Number(cursor ?? 0);
      return Promise.resolve({ items: Array.from({ length: 100 }, (_, index) => structure(String(offset + index))),
        next_cursor: String(offset + 100) });
    });
    const result = await fetchOverviewData();
    expect(result.structures.status).toBe("partial");
    expect(result.structures.data).toHaveLength(10_000);
    expect(result.structures.message).toBeTruthy();
    expect(api.list).toHaveBeenCalledTimes(100);
  });

  it("bounds empty pages with advancing cursors instead of treating them as a complete zero", async () => {
    let page = 0;
    api.list.mockImplementation(() => Promise.resolve({ items: [], next_cursor: `empty-page-${++page}` }));
    const result = await fetchOverviewData();
    expect(api.list).toHaveBeenCalledTimes(100);
    expect(result.structures).toMatchObject({ status: "partial", data: [] });
    expect(result.structures.message).toBeTruthy();
  });

  it("keeps independent structure, task and usage data when knowledge sources fail", async () => {
    api.knowledge.mockRejectedValue(new Error("知识来源暂不可用"));
    api.list.mockResolvedValue({ items: [structure("retained")], next_cursor: null });
    const result = await fetchOverviewData();
    expect(result.knowledge).toMatchObject({ status: "unavailable", data: null });
    expect(result.structures.data?.map(item => item.id)).toEqual(["retained"]);
    expect(result.taskSources).toEqual(taskSources);
    expect(result.knowledgeActivity.status).toBe("ready");
  });

  it("keeps other sources when native task loading fails without inventing zero totals", async () => {
    api.tasks.mockRejectedValue(new Error("任务服务暂不可用"));
    const result = await fetchOverviewData();
    expect(result.taskSources.map(source => source.id).sort()).toEqual(["monomerDft", "monomerMd", "polymerMd", "polymerizationBatch"]);
    for (const source of result.taskSources) {
      expect(source).toMatchObject({ status: "unavailable", total: null, tasks: [] });
      expect(source.message).toBeTruthy();
    }
    expect(result.structures.status).toBe("ready");
    expect(result.knowledge.status).toBe("ready");
    expect(result.knowledgeActivity.status).toBe("ready");
  });

  it.each([
    { enabled: true, ready: false },
    { enabled: false, ready: false },
    { enabled: false, ready: true }
  ])("preserves knowledge availability metadata and hides unusable totals: %o", async flags => {
    api.knowledge.mockResolvedValue(knowledge([
      { data_source: "legacy", enabled: true, ready: true, total: 12 },
      { data_source: "extracted_literature", ...flags, total: 999 }
    ]));
    const result = await fetchOverviewData();
    expect(result.knowledge.status).toBe("partial");
    expect(result.knowledge.data?.sources).toEqual([
      expect.objectContaining({ id: "legacy", ready: true, enabled: true, total: 12 }),
      expect.objectContaining({ id: "extracted_literature", ...flags, total: null })
    ]);
  });

  it("retains source metadata when every knowledge source is unavailable", async () => {
    api.knowledge.mockResolvedValue(knowledge([
      { data_source: "legacy", enabled: true, ready: false, total: 99 },
      { data_source: "extracted_literature", enabled: false, ready: false, total: 99 }
    ]));
    const result = await fetchOverviewData();
    expect(result.knowledge.status).toBe("unavailable");
    expect(result.knowledge.data?.sources).toEqual([
      expect.objectContaining({ id: "legacy", enabled: true, ready: false, total: null }),
      expect.objectContaining({ id: "extracted_literature", enabled: false, ready: false, total: null })
    ]);
  });

  it.each(["summary", "activity"] as const)("keeps knowledge activity partial when only %s fails", async source => {
    api[source].mockRejectedValue(new Error("用量来源暂不可用"));
    const result = await fetchOverviewData();
    expect(result.knowledgeActivity.status).toBe("partial");
    expect(result.knowledgeActivity.data).toMatchObject({ dailyAvailable: source !== "summary", recentAvailable: source !== "activity" });
    if (source === "summary") {
      expect(result.knowledgeActivity.data?.daily).toEqual([]);
      expect(result.knowledgeActivity.data?.recent).toEqual([expect.objectContaining({
        status: "completed", submittedAt: timestamp, updatedAt: timestamp
      })]);
    } else {
      expect(result.knowledgeActivity.data?.recent).toEqual([]);
      expect(result.knowledgeActivity.data?.daily).toEqual([{ date: "2026-10-21", count: 3 }]);
    }
    expect(result.knowledge.status).toBe("ready");
  });

  it("marks knowledge activity unavailable when both usage sources fail", async () => {
    api.summary.mockRejectedValue(new Error("统计未启用"));
    api.activity.mockRejectedValue(new Error("历史不可用"));
    const result = await fetchOverviewData();
    expect(result.knowledgeActivity).toMatchObject({ status: "unavailable", data: null });
    expect(result.structures.status).toBe("ready");
  });

  it("anchors asOf to the requested usage period when a read crosses Beijing midnight", async () => {
    vi.useFakeTimers();
    const startedAt = new Date("2031-02-03T15:59:59.000Z");
    const finishedAt = new Date("2031-02-03T16:00:04.000Z");
    vi.setSystemTime(startedAt);
    const page = deferred<{ items: StructureRecordSummary[]; next_cursor: string | null }>();
    api.list.mockReturnValue(page.promise);
    const pending = fetchOverviewData();
    vi.setSystemTime(finishedAt);
    page.resolve({ items: [], next_cursor: null });
    const result = await pending;
    expect(result.asOf).toBe(startedAt.toISOString());
    const [period] = api.summary.mock.calls[0];
    expect(Date.parse(period.end) - Date.parse(period.start)).toBe(365 * 86_400_000);
    expect(Date.parse(period.end)).toBe(startedAt.getTime());
    expect(result.asOf).toBe(period.end);
    const localDay = (value: string) => new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(value));
    expect(localDay(result.asOf)).toBe("2031-02-03");
    expect(localDay(finishedAt.toISOString())).toBe("2031-02-04");
    expect(period.timezone).toBe("Asia/Shanghai");
  });

  it("propagates an aborted source instead of converting cancellation to unavailable or zero", async () => {
    api.summary.mockRejectedValue(new DOMException("取消读取", "AbortError"));
    await expect(fetchOverviewData()).rejects.toMatchObject({ name: "AbortError" });
  });

  it.each(["abort", "retire"] as const)("rejects all late overview data after %s", async action => {
    const page = deferred<{ items: StructureRecordSummary[]; next_cursor: string | null }>();
    api.list.mockReturnValue(page.promise);
    const controller = new AbortController();
    const pending = fetchOverviewData(controller.signal);
    if (action === "abort") controller.abort();
    else { retireSession(); member("overview-b"); }
    page.resolve({ items: [structure("previous-account")], next_cursor: null });
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
});
