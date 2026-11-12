import { assertSessionEpoch, getSessionEpoch, privateFetch, staleSessionError } from "../auth/session";
import type { OverviewTask, OverviewTaskSource } from "../types/overview";
import { API_BASE_URL, fetchMonomerDftJobs, fetchMonomerMdJobs } from "./api";

const PAGE_SIZE = 100;
const MAX_RECORDS = 10_000;
type Row = Record<string, unknown>;
type SourceId = OverviewTask["source"];
type Source = { id: SourceId; label: string; module: OverviewTask["module"]; offset?: boolean };
const sources: Source[] = [
  { id: "monomerMd", label: "单体 MD", module: "monomerMdSimulation" },
  { id: "monomerDft", label: "单体 DFT", module: "monomerDft" },
  { id: "polymerMd", label: "聚合物 MD", module: "mdSimulationDemo" },
  { id: "polymerizationBatch", label: "批量聚合", module: "monomerPolymerization", offset: true }
];

const statusLabels: Record<string, string> = {
  pending: "等待提交", submitted: "已提交", queued: "排队中", running: "计算中",
  cancel_requested: "取消中", cancelling: "取消中", completed: "已完成",
  completed_with_errors: "已完成，部分结果有误", failed: "计算失败", cancelled: "已取消", expired: "结果已过期"
};
const allowedStatuses: Record<SourceId, Set<string>> = {
  monomerMd: new Set(["pending", "submitted", "running", "cancel_requested", "completed", "failed", "cancelled"]),
  monomerDft: new Set(["pending", "queued", "running", "cancel_requested", "completed", "failed", "cancelled"]),
  polymerMd: new Set(["pending", "queued", "running", "cancel_requested", "completed", "failed", "cancelled"]),
  polymerizationBatch: new Set(["queued", "running", "cancelling", "completed", "completed_with_errors", "failed", "cancelled", "expired"])
};
const activeStatuses = new Set(["pending", "submitted", "queued", "running", "cancel_requested", "cancelling"]);
const stages: Record<string, string> = {
  pending: "等待执行", queued: "排队中", validating: "校验输入", preparing: "准备计算",
  conformer: "构象生成", single_point: "单点能计算", optimization: "结构优化",
  hessian: "Hessian 计算", frequency: "振动频率计算", artifacts: "整理结果文件",
  qm: "量子化学计算", terminal_qm: "端基量子化学", monomer_qm: "重复单元量子化学",
  building: "聚合物建模", build: "聚合物建模", eq1: "EQ1 松弛与装填", eq2: "EQ2 压缩与解压",
  eq3: "EQ3 平衡采样", eq: "平衡模拟", additional: "追加平衡", basic: "基础物性分析",
  analyze: "基础物性分析", tc: "RNEMD 热导率", rnemd: "RNEMD 热导率",
  sp: "SP 采样与能量重算", sp_analyze: "SP 与内聚能分析", tg: "Tg 分段降温",
  tg_analyze: "Tg 拟合与质量评估", tc_analyze: "热导率质量评估", completed: "结果汇总完成",
  classify: "单体分类", generate: "生成聚合物", export: "整理输出文件", finished: "任务结束"
};
const protocols: Record<string, string> = {
  Density: "密度计算", Transport: "输运性质计算", HVap: "汽化焓计算",
  Dielectric: "介电常数计算", Compressibility: "压缩率计算"
};
const targetClasses: Record<string, string> = {
  polyolefin: "聚烯烃", polyester: "聚酯", polyether: "聚醚", polyamide: "聚酰胺",
  polyimide: "聚酰亚胺", polyurethane: "聚氨酯", polyoxazolidone: "聚噁唑烷酮", all: "全类别"
};

const record = (value: unknown): value is Row => !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max = 128): string | null => typeof value === "string" && value.trim().length > 0 && value.length <= max ? value.trim() : null;
const integer = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
function timestamp(value: unknown): string | null {
  const input = text(value, 80);
  if (!input || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(input)) return null;
  const date = new Date(input);
  return Number.isFinite(date.getTime()) ? date.toISOString() : null;
}
function stageLabel(value: unknown): string | null {
  const stage = text(value, 64);
  if (!stage) return null;
  if (/^additional_\d{1,3}$/.test(stage)) return `第 ${stage.split("_")[1]} 次追加平衡`;
  if (/^analyze_\d{1,3}$/.test(stage)) return stage === "analyze_0" ? "基础物性分析" : "追加平衡后的物性分析";
  // Worker stage strings are not uniformly typed. Publish known labels only,
  // never forward a path, exception fragment or other unexpected worker text.
  return Object.hasOwn(stages, stage) ? stages[stage] : null;
}

function normalize(source: Source, value: unknown): { task: OverviewTask | null; incomplete: boolean } {
  if (!record(value)) return { task: null, incomplete: true };
  const jobId = text(value.job_id, 256);
  // The query already excludes demos; validate the response too before counting it.
  if (!jobId || (source.id === "monomerMd" && (value.run_mode !== "formal" || value.protocol === "DensityDemo"))) {
    return { task: null, incomplete: true };
  }
  const status = text(value.status, 64) ?? "unknown";
  const knownStatus = allowedStatuses[source.id].has(status);
  let outcome: OverviewTask["outcome"] = !knownStatus ? "unknown" : activeStatuses.has(status) ? "active"
    : status === "completed" ? "completed" : status === "cancelled" ? "cancelled" : "attention";
  let statusLabel = knownStatus ? statusLabels[status] : "状态待确认";
  if (source.id === "polymerMd" && status === "completed" && ["warning", "not_converged", "failed"].includes(String(value.quality_status))) {
    outcome = "attention";
    statusLabel = "已完成，质量需关注";
  }
  const suffix = jobId.slice(0, 8);
  let title = `${source.label} · ${suffix}`;
  if (source.id === "monomerMd") {
    const protocol = String(value.protocol);
    title = `${Object.hasOwn(protocols, protocol) ? protocols[protocol] : "单体 MD"} · ${suffix}`;
  }
  if (source.id === "monomerDft") title = `${value.calculation_type === "single_point" ? "单点能计算" : value.calculation_type === "optimization" ? "结构优化" : "单体 DFT"} · ${suffix}`;
  if (source.id === "polymerMd" && record(value.scientific_request)) title = `聚合物 ${value.scientific_request.calculation === "basic_tc" ? "导热计算" : "MD"} · ${suffix}`;
  if (source.id === "polymerizationBatch") {
    const targetClass = String(value.target_class);
    title = `${Object.hasOwn(targetClasses, targetClass) ? targetClasses[targetClass] : "批量"}聚合 · ${suffix}`;
  }
  const createdAt = timestamp(value.created_at), updatedAt = timestamp(value.updated_at), finishedAt = timestamp(value.finished_at);
  const reportedPercent = value.progress_percent;
  const percentSource = source.id === "monomerMd" || source.id === "monomerDft";
  const progress = percentSource && typeof reportedPercent === "number" && Number.isFinite(reportedPercent) && reportedPercent >= 0 && reportedPercent <= 100 ? reportedPercent : null;
  const invalidTime = !createdAt || (value.updated_at != null && !updatedAt) || (value.finished_at != null && !finishedAt);
  const invalidPercent = percentSource && reportedPercent != null && progress === null;
  return {
    incomplete: !knownStatus || invalidTime || invalidPercent,
    task: { id: `${source.id}:${jobId}`, jobId, source: source.id, title, module: source.module,
      status, statusLabel, outcome, progress,
      stage: stageLabel(source.id === "monomerMd" ? value.progress_stage : value.stage),
      createdAt, updatedAt, finishedAt }
  };
}

async function metadata(path: string, signal?: AbortSignal): Promise<unknown> {
  const response = await privateFetch(`${API_BASE_URL}${path}`, { signal, cache: "no-store" });
  if (!response.ok) throw new Error("任务列表暂时不可用。");
  return response.json();
}
async function pageFor(source: Source, cursor: number, signal?: AbortSignal): Promise<unknown> {
  if (source.id === "monomerMd") return fetchMonomerMdJobs({ run_mode: "formal", include_result: false, page: cursor, page_size: PAGE_SIZE }, signal);
  if (source.id === "monomerDft") return fetchMonomerDftJobs({ page: cursor, page_size: PAGE_SIZE }, signal);
  // These public list endpoints allow 100 rows; their existing workspace wrappers fix a smaller page size.
  if (source.id === "polymerMd") return metadata(`/polymer-md/jobs?page=${cursor}&page_size=${PAGE_SIZE}`, signal);
  return metadata(`/monomer-polymerization/batch/jobs?offset=${cursor}&limit=${PAGE_SIZE}`, signal);
}

async function loadSource(source: Source, epoch: number, signal?: AbortSignal): Promise<OverviewTaskSource> {
  const tasks: OverviewTask[] = [];
  const ids = new Set<string>();
  const issues = new Set<string>();
  let total: number | null = null, cursor = source.offset ? 0 : 1, readCount = 0;
  const check = () => {
    assertSessionEpoch(epoch);
    if (signal?.aborted) throw staleSessionError();
  };
  const result = (status: OverviewTaskSource["status"]): OverviewTaskSource => ({
    id: source.id, label: source.label, status, total: status === "unavailable" ? null : total, tasks,
    ...(issues.size ? { message: [...issues].join("；") } : {})
  });
  try {
    // Both row count and request count are bounded, including malformed/empty pages.
    for (let requestCount = 0; requestCount < MAX_RECORDS / PAGE_SIZE; requestCount += 1) {
      check();
      const page = await pageFor(source, cursor, signal);
      check();
      if (!record(page) || !Array.isArray(page.items)) throw new Error("invalid_page");
      const reportedTotal = page.total;
      if (integer(reportedTotal)) {
        if (total !== null && total !== reportedTotal) issues.add("读取期间任务总数发生变化");
        total = reportedTotal;
      } else issues.add("任务总数暂无法确认");
      let duplicate = false;
      for (const item of page.items.slice(0, Math.min(PAGE_SIZE, MAX_RECORDS - readCount))) {
        readCount += 1;
        const normalized = normalize(source, item);
        if (normalized.incomplete) issues.add("部分任务字段或状态待确认");
        if (!normalized.task) continue;
        if (ids.has(normalized.task.id)) { duplicate = true; continue; }
        ids.add(normalized.task.id);
        tasks.push(normalized.task);
      }
      if (duplicate) { issues.add("任务分页包含重复记录"); return result("partial"); }
      if (page.items.length > PAGE_SIZE) { issues.add("任务分页超出约定范围"); return result("partial"); }
      if (!integer(reportedTotal)) return result(tasks.length ? "partial" : "unavailable");
      if (readCount > reportedTotal) { issues.add("任务分页数量与总数不一致"); return result("partial"); }
      if (source.offset) {
        const next = page.next_offset;
        if (next === null) {
          if (readCount !== reportedTotal) issues.add("任务分页提前结束");
          return result(issues.size ? "partial" : "ready");
        }
        if (!integer(next) || next <= cursor || next !== cursor + page.items.length || page.items.length === 0 || readCount >= reportedTotal) {
          issues.add("任务分页游标未正常前进");
          return result("partial");
        }
        cursor = next;
      } else {
        if (!integer(page.page) || page.page !== cursor || !integer(page.page_size) || page.page_size < 1 || page.page_size > PAGE_SIZE || page.items.length > page.page_size) {
          issues.add("任务分页信息不完整");
          return result("partial");
        }
        if (readCount === reportedTotal) return result(issues.size ? "partial" : "ready");
        if (page.items.length < page.page_size) { issues.add("任务分页提前结束"); return result("partial"); }
        cursor += 1;
      }
      if (readCount >= MAX_RECORDS) { issues.add("已读取每类最多 10,000 条记录，统计尚不完整"); return result("partial"); }
    }
    issues.add("已达到任务分页读取上限，统计尚不完整");
    return result("partial");
  } catch (error) {
    check();
    if (error && typeof error === "object" && "name" in error && error.name === "AbortError") throw error;
    issues.add(tasks.length ? "部分任务列表未能读取，请重试" : "任务列表暂时不可用，请重试");
    return result(tasks.length || total !== null ? "partial" : "unavailable");
  }
}

/** Personal, retained formal tasks only. Never starts work or reads result artifacts. */
export async function fetchOverviewTasks(signal?: AbortSignal): Promise<OverviewTaskSource[]> {
  const epoch = getSessionEpoch();
  const result = await Promise.all(sources.map(source => loadSource(source, epoch, signal)));
  assertSessionEpoch(epoch);
  if (signal?.aborted) throw staleSessionError();
  return result;
}
