import type { ActiveModule } from "../routing";
import type { OverviewData, OverviewTask } from "../types/overview";

export type OverviewDetail = { title: string; eyebrow: string; description: string; facts: [string, string][]; module: ActiveModule; action: string };
export type OverviewEvent = { id: string; title: string; description: string; at: string; kind: "structure" | "task" | "knowledge"; tone: string; detail: OverviewDetail };

const dayFormatter = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });
const dateFormatter = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const percentFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });

export function dayKey(value: string | Date): string | null {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  const parts = dayFormatter.formatToParts(date);
  const part = (name: string) => parts.find(value => value.type === name)!.value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

export function dateText(value: string | null) {
  const date = value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? dateFormatter.format(date) : "暂无时间记录";
}

export const progressText = (value: number) => `${percentFormatter.format(value)}%`;

export function taskDetail(task: OverviewTask): OverviewDetail {
  const facts: [string, string][] = [["任务编号", task.jobId], ["当前状态", task.statusLabel], ["提交时间", dateText(task.createdAt)], ["更新时间", dateText(task.updatedAt)]];
  if (task.stage) facts.splice(2, 0, ["当前阶段", task.stage]);
  if (task.progress !== null) facts.push(["任务进度", progressText(task.progress)]);
  return { title: task.title, eyebrow: "当前账号 · 计算任务", description: "状态与进度来自对应计算模块。进入模块可查看完整任务记录和已有结果。", facts, module: task.module, action: "打开计算模块" };
}

export function buildOverviewModel(data: OverviewData | null, period: number) {
  const today = dayKey(data?.asOf ?? new Date())!;
  const snapshot = new Date(`${today}T00:00:00Z`);
  const start = new Date(snapshot); start.setUTCDate(start.getUTCDate() - period + 1);
  const periodKey = start.toISOString().slice(0, 10);
  const inPeriod = (value: string | null) => { const day = value && dayKey(value); return Boolean(day && day >= periodKey && day <= today); };
  const onToday = (value: string | null) => Boolean(value && dayKey(value) === today);
  const taskSources = data?.taskSources ?? [];
  const tasks = taskSources.flatMap(source => source.tasks);
  const taskReady = taskSources.length === 4 && taskSources.every(source => source.status === "ready");
  const structureReady = data?.structures.status === "ready";
  const structures = data?.structures.data ?? [];
  const knowledgeDailyReady = Boolean(data?.knowledgeActivity.data?.dailyAvailable);
  const knowledgeDaily = data?.knowledgeActivity.data?.daily ?? [];
  const totalTasks = taskReady && taskSources.every(source => source.total !== null)
    ? taskSources.reduce((sum, source) => sum + source.total!, 0) : null;
  const active = tasks.filter(task => task.outcome === "active");
  const completed = tasks.filter(task => task.outcome === "completed" || task.status === "completed" || task.status === "completed_with_errors");
  const attention = tasks.filter(task => task.outcome === "attention");
  const byRecent = (left: OverviewTask, right: OverviewTask) => (Date.parse(right.updatedAt ?? right.createdAt ?? "") || 0) - (Date.parse(left.updatedAt ?? left.createdAt ?? "") || 0);
  const recentTasks = [...tasks].sort((left, right) => Number(right.outcome === "active") - Number(left.outcome === "active") || byRecent(left, right)).slice(0, 3);
  const latestCompleted = [...completed].sort(byRecent)[0];
  const latestAttention = [...attention].sort(byRecent)[0];
  const knowledgeSources = data?.knowledge.data?.sources ?? [];
  const literature = knowledgeSources.find(source => source.id === "extracted_literature" && source.enabled && source.ready && source.total !== null);
  const legacy = knowledgeSources.find(source => source.id === "legacy" && source.enabled && source.ready && source.total !== null);
  const primaryKnowledge = literature ?? legacy;
  const events: OverviewEvent[] = [];
  for (const record of structures) {
    if (!record.createdAt || !dayKey(record.createdAt)) continue;
    const detail: OverviewDetail = { title: record.name, eyebrow: "当前账号 · 已保存结构", description: "这条结构已保存到当前账号的结构库，可在结构工作台继续使用。", facts: [["结构编号", record.id], ["保存时间", dateText(record.createdAt)], ["更新时间", dateText(record.updatedAt)]], module: "structureWorkbench", action: "打开结构工作台" };
    events.push({ id: `structure:${record.id}`, title: "保存了新的材料结构", description: record.name, at: record.createdAt, kind: "structure", tone: "violet", detail });
  }
  for (const task of tasks) {
    const at = task.updatedAt ?? task.createdAt;
    if (!at || !dayKey(at)) continue;
    events.push({ id: task.id, title: `${task.title} · ${task.statusLabel}`, description: task.stage ?? `任务 ${task.jobId}`, at, kind: "task", tone: task.outcome === "attention" ? "amber" : "cyan", detail: taskDetail(task) });
  }
  for (const record of data?.knowledgeActivity.data?.recent ?? []) {
    const at = record.updatedAt ?? record.submittedAt;
    if (!at || !dayKey(at)) continue;
    events.push({ id: `knowledge:${record.id}`, title: record.title, description: record.statusLabel, at, kind: "knowledge", tone: "blue", detail: { title: record.title, eyebrow: "当前账号 · 知识检索记录", description: "此记录来自当前账号已记录的知识检索活动。", facts: [["任务编号", record.id], ["任务状态", record.statusLabel], ["提交时间", dateText(record.submittedAt)]], module: "knowledge", action: "打开知识检索" } });
  }
  events.sort((left, right) => Date.parse(right.at) - Date.parse(left.at));
  const countByDay = (dates: Array<string | null>) => {
    const result = new Map<string, number>();
    for (const value of dates) { const day = value && dayKey(value); if (day) result.set(day, (result.get(day) ?? 0) + 1); }
    return result;
  };
  const structureDays = countByDay(structures.map(record => record.createdAt));
  const taskDays = countByDay(tasks.map(task => task.createdAt));
  const knowledgeDays = new Map(knowledgeDaily.map(day => [day.date, day.count]));
  const structureActivityReady = structureReady && structures.every(record => Boolean(record.createdAt && dayKey(record.createdAt)));
  const taskActivityReady = taskReady && tasks.every(task => Boolean(task.createdAt && dayKey(task.createdAt)));
  const series = (values: Map<string, number>) => Array.from({ length: period }, (_, index) => {
    const date = new Date(start); date.setUTCDate(date.getUTCDate() + index); return values.get(date.toISOString().slice(0, 10)) ?? 0;
  });
  return {
    today, snapshot, taskReady, structureReady, structureActivityReady, taskActivityReady, knowledgeDailyReady, totalTasks, tasks, active, completed, attention, recentTasks,
    runningCount: taskReady ? active.length : null,
    completedToday: taskReady && completed.every(task => Boolean(task.finishedAt && dayKey(task.finishedAt))) ? completed.filter(task => onToday(task.finishedAt)).length : null,
    savedCount: structureReady ? structures.length : null,
    todayStructures: structureActivityReady ? structures.filter(record => onToday(record.createdAt)).length : null,
    todayKnowledge: knowledgeDailyReady ? knowledgeDays.get(today) ?? 0 : null,
    submittedInPeriod: taskActivityReady ? tasks.filter(task => inPeriod(task.createdAt)).length : null,
    structuresInPeriod: structureActivityReady ? structures.filter(record => inPeriod(record.createdAt)).length : null,
    primaryKnowledge, literature, legacy, knowledgeSources, latestCompleted, latestAttention,
    events, structureDays, taskDays, knowledgeDays,
    taskSeries: taskActivityReady ? series(taskDays) : null,
    structureSeries: structureActivityReady ? series(structureDays) : null,
    knowledgeSeries: knowledgeDailyReady ? series(knowledgeDays) : null,
  };
}

export type OverviewModel = ReturnType<typeof buildOverviewModel>;

export function buildActivityDays(model: OverviewModel, source: string) {
  const start = new Date(model.snapshot); start.setUTCDate(start.getUTCDate() - 364);
  const first = new Date(start); first.setUTCDate(first.getUTCDate() - first.getUTCDay());
  return Array.from({ length: 371 }, (_, index) => {
    const date = new Date(first); date.setUTCDate(date.getUTCDate() + index);
    const key = date.toISOString().slice(0, 10);
    const inRange = date >= start && date <= model.snapshot;
    const breakdown = [model.structureActivityReady ? model.structureDays.get(key) ?? 0 : null, model.taskActivityReady ? model.taskDays.get(key) ?? 0 : null, model.knowledgeDailyReady ? model.knowledgeDays.get(key) ?? 0 : null];
    const available = source === "all" ? breakdown.filter((count): count is number => count !== null) : breakdown[Number(source)] === null ? [] : [breakdown[Number(source)]!];
    const count = available.length ? available.reduce((sum, value) => sum + value, 0) : null;
    return { date, key, inRange, breakdown, count, level: count === null || count === 0 ? 0 : count < 3 ? 1 : count < 6 ? 2 : count < 9 ? 3 : 4 };
  });
}
