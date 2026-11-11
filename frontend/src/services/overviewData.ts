import { assertSessionEpoch, getSessionEpoch, staleSessionError } from "../auth/session";
import { structureLibraryApi } from "../structure-library/api";
import type { OverviewData, OverviewKnowledge, OverviewKnowledgeActivity, OverviewSource, OverviewStructure, OverviewTaskSource } from "../types/overview";
import type { UsageStatus } from "../types/usage";
import { fetchKnowledgeSources } from "./api";
import { fetchOverviewTasks } from "./overviewTasks";
import { fetchUsageActivityByGroup, fetchUsageSummary } from "./usage";

const MAX_STRUCTURES = 10_000;
const STRUCTURE_PAGE_SIZE = 100;
const MAX_STRUCTURE_PAGES = MAX_STRUCTURES / STRUCTURE_PAGE_SIZE;
const DAY_MS = 86_400_000;
const statusLabels: Record<UsageStatus, string> = {
  queued: "排队中", running: "运行中", completed: "已完成", failed: "失败",
  cancelled: "已取消", timed_out: "超时", interrupted: "已中断", rejected: "已拒绝",
};
const taskLabels = { monomerMd: "单体 MD", monomerDft: "单体 DFT", polymerMd: "聚合物 MD", polymerizationBatch: "批量聚合" } as const;
const message = (error: unknown, fallback: string) => error instanceof Error && error.message ? `${fallback}：${error.message}` : fallback;
const isAbort = (error: unknown) => error instanceof Error && error.name === "AbortError";
const timestamp = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value)) ? value : null;

/** Every source is independent, but cancellation or a retired identity invalidates the whole snapshot. */
export async function fetchOverviewData(signal?: AbortSignal): Promise<OverviewData> {
  const epoch = getSessionEpoch();
  const check = () => {
    assertSessionEpoch(epoch);
    if (signal?.aborted) throw staleSessionError();
  };
  const failed = (error: unknown) => {
    check();
    if (isAbort(error)) throw error;
  };
  check();
  const now = new Date();

  const structures = async (): Promise<OverviewSource<OverviewStructure[]>> => {
    const records = new Map<string, OverviewStructure>();
    const cursors = new Set<string>();
    let cursor: string | null = null;
    let pages = 0;
    try {
      while (true) {
        check();
        const page = await structureLibraryApi.list("", cursor, signal, STRUCTURE_PAGE_SIZE);
        check();
        pages += 1;
        for (const record of page.items) {
          if (records.size >= MAX_STRUCTURES && !records.has(record.id)) break;
          records.set(record.id, { id: record.id, name: record.name, createdAt: timestamp(record.created_at), updatedAt: timestamp(record.updated_at) });
        }
        if (page.items.some(record => !records.has(record.id)) || (records.size >= MAX_STRUCTURES && page.next_cursor)) {
          return { status: "partial", data: [...records.values()], message: "已读取 10000 条结构保存记录，更多记录尚未统计。" };
        }
        if (!page.next_cursor) return { status: "ready", data: [...records.values()] };
        if (cursors.has(page.next_cursor) || pages >= MAX_STRUCTURE_PAGES) {
          return { status: "partial", data: [...records.values()], message: "结构库分页未完成，当前仅展示已读取的保存记录。" };
        }
        cursors.add(page.next_cursor);
        cursor = page.next_cursor;
      }
    } catch (error) {
      failed(error);
      return { status: pages ? "partial" : "unavailable", data: pages ? [...records.values()] : null, message: message(error, "结构库暂时无法完整读取") };
    }
  };

  const tasks = async (): Promise<OverviewTaskSource[]> => {
    try { const result = await fetchOverviewTasks(signal); check(); return result; }
    catch (error) {
      failed(error);
      return Object.entries(taskLabels).map(([id, label]) => ({ id: id as OverviewTaskSource["id"], label, status: "unavailable", total: null, tasks: [], message: message(error, "计算任务暂时无法读取") }));
    }
  };

  const knowledge = async (): Promise<OverviewSource<OverviewKnowledge>> => {
    try {
      const result = await fetchKnowledgeSources(signal);
      check();
      const sources = result.sources.map(source => ({
        id: source.data_source,
        label: source.data_source === "extracted_literature" ? "提取文献" : "基础知识库",
        ready: source.ready,
        enabled: source.enabled,
        total: source.enabled && source.ready && Number.isFinite(source.total) && source.total >= 0 ? source.total : null,
      }));
      const available = sources.filter(source => source.total !== null).length;
      const status = available === sources.length && available > 0 ? "ready" : available ? "partial" : "unavailable";
      return { status, data: { sources }, ...(status === "ready" ? {} : { message: "部分知识来源未启用或尚未就绪，未就绪来源不计为零。" }) };
    } catch (error) {
      failed(error);
      return { status: "unavailable", data: null, message: message(error, "知识来源暂时无法读取") };
    }
  };

  const activity = async (): Promise<OverviewSource<OverviewKnowledgeActivity>> => {
    const [summary, recent] = await Promise.allSettled([
      fetchUsageSummary({ start: new Date(now.getTime() - 365 * DAY_MS).toISOString(), end: now.toISOString(), module_group: "knowledge", timezone: "Asia/Shanghai" }, signal),
      fetchUsageActivityByGroup(0, "knowledge", signal),
    ]);
    check();
    if (summary.status === "rejected") failed(summary.reason);
    if (recent.status === "rejected") failed(recent.reason);
    if (summary.status === "rejected" && recent.status === "rejected") {
      return { status: "unavailable", data: null, message: message(summary.reason, "知识活动统计暂时不可用") };
    }
    const warnings = [
      ...(summary.status === "fulfilled" ? summary.value.coverage.warnings : [message(summary.reason, "知识活动日统计暂时不可用")]),
      ...(recent.status === "fulfilled" ? recent.value.warnings : [message(recent.reason, "最近知识活动暂时不可用")]),
    ];
    const partial = summary.status === "rejected" || recent.status === "rejected";
    return {
      status: partial ? "partial" : "ready",
      data: {
        dailyAvailable: summary.status === "fulfilled",
        recentAvailable: recent.status === "fulfilled",
        daily: summary.status === "fulfilled" ? summary.value.daily.map(day => ({ date: day.date, count: day.tasks })) : [],
        recent: recent.status === "fulfilled" ? recent.value.items.map(item => ({
          id: `${item.module}:${item.task_id}`,
          title: item.module === "online_knowledge" ? "在线知识抽取" : "知识检索",
          status: item.status,
          statusLabel: statusLabels[item.status] ?? item.status,
          submittedAt: timestamp(item.submitted_at), updatedAt: timestamp(item.updated_at),
        })) : [],
        warnings: [...new Set(warnings)],
      },
      ...(partial ? { message: warnings.join("；") } : {}),
    };
  };

  const [structureData, taskSources, knowledgeData, knowledgeActivity] = await Promise.all([structures(), tasks(), knowledge(), activity()]);
  check();
  return { asOf: now.toISOString(), structures: structureData, taskSources, knowledge: knowledgeData, knowledgeActivity };
}
