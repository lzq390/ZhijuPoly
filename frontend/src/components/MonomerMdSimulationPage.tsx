import {
  Activity,
  ArrowLeft,
  Atom,
  CheckCircle2,
  Database,
  FileJson,
  Gauge,
  Loader2,
  Play,
  RotateCw,
  Timer,
  Trash2,
  TriangleAlert,
  XCircle
} from "lucide-react";
import { type FormEvent } from "react";
import {
  getMonomerMdSmilesValidationError,
  useMonomerMdSimulation
} from "../hooks/useMonomerMdSimulation";
import { cn } from "../lib/utils";
import type {
  MonomerMdArtifact,
  MonomerMdJobResponse,
  MonomerMdJobStatus,
  MonomerMdProtocol,
  MonomerMdSeries,
  MonomerMdSimulationResult,
  MonomerMdTrajectoryPoint,
  MonomerMdTrajectoryPreview
} from "../types";
import { isGenericMonomerMdDemoWarning, monomerMdDemoNotice, monomerMdServiceCanSubmit } from "../utils/monomerMdPresentation";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Textarea } from "./ui/textarea";

type MonomerMdSimulationPageProps = {
  onBackHome: () => void;
};

type PlotPoint = {
  x: number;
  y: number;
};

const STATUS_LABELS: Record<MonomerMdJobStatus, string> = {
  pending: "等待提交",
  submitted: "已提交",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消"
};

const STATUS_STEPS: MonomerMdJobStatus[] = ["pending", "submitted", "running", "completed"];
const FORMAL_PROTOCOLS: MonomerMdProtocol[] = ["Density", "HVap", "Compressibility", "Dielectric", "Transport"];
const PROTOCOL_LABELS: Record<MonomerMdProtocol, string> = {
  DensityDemo: "DensityDemo",
  Density: "Density",
  HVap: "HVap",
  Compressibility: "Compressibility",
  Dielectric: "Dielectric",
  Transport: "Transport"
};
const SUMMARY_LABELS: Record<string, string> = {
  final_density_g_cm3: "最终密度",
  mean_density_g_cm3: "平均密度",
  mean_temperature_k: "平均温度",
  final_temperature_k: "最终温度",
  mean_total_energy_kcal_mol: "平均总能量",
  final_total_energy_kcal_mol: "最终总能量",
  elapsed_seconds: "耗时",
  n_atoms: "原子数",
  n_frames: "帧数",
  n_steps: "步数",
  density: "密度",
  density_std: "密度标准差",
  hvap: "汽化焓",
  hvap_std: "汽化焓标准差",
  dielectric: "介电常数",
  compressibility: "等温压缩率",
  viscosity: "粘度"
};

const RESULT_MESSAGE_TRANSLATIONS: Record<string, string> = {
  "300-step demo output is not equilibrated and is not a physical density estimate.": "300 步演示结果尚未达到平衡，不能作为物理密度估计。",
  "1000-step demo output is not equilibrated and is not a physical density estimate.": "1000 步演示结果尚未达到平衡，不能作为物理密度估计。",
  "Density demo output is not equilibrated and is not a physical density estimate.": "密度演示输出尚未达到平衡，不能作为物理密度估计。"
};

const UI_MESSAGE_TRANSLATIONS: Record<string, string> = {
  "Backend reports that monomer MD service is unavailable.": "后端报告单体 MD 服务当前不可用。",
  "Request validation failed with status 422": "请求参数校验失败，请检查输入的 SMILES。",
  "Request failed with status 422": "请求参数校验失败，请检查输入的 SMILES。",
  "monomer MD submissions are disabled": "单体 MD 提交功能当前已关闭。",
  "monomer MD worker is ready": "单体 MD worker 已就绪。",
  "monomer MD worker is disabled until MONOMER_MD_WORKER_BASE_URL is configured": "单体 MD worker 尚未启用，请配置 MONOMER_MD_WORKER_BASE_URL。",
  "monomer MD worker is not configured": "单体 MD worker 尚未配置。",
  "monomer MD worker is not reachable": "无法连接单体 MD worker。",
  "monomer MD worker database is not configured": "单体 MD worker 数据库尚未配置。",
  "monomer MD worker ByteFF2 root is not available": "单体 MD worker 找不到 ByteFF2 根目录。",
  "monomer MD worker runtime is not ready": "单体 MD worker 运行环境尚未就绪。",
  "monomer MD submit rate limit exceeded; please wait before submitting another job": "提交过于频繁，请稍后再试。",
  "monomer MD job capacity is full; please wait for the active job to finish": "当前已有单体 MD 任务在运行，请等待完成后再提交。",
  "monomer MD job capacity is full; please wait for the current demo job to finish": "当前已有单体 MD 任务在运行，请等待完成后再提交。",
  "monomer MD worker is draining for deployment": "单体 MD worker 正在等待现有任务完成并进行部署升级。",
  "monomer MD worker is not accepting jobs": "单体 MD worker 当前暂不接收新任务。",
  "monomer MD worker database recovery has not completed": "单体 MD worker 正在恢复任务状态，请稍后重试。",
  "monomer MD database capacity check failed": "无法读取单体 MD 任务容量，请稍后重试。",
  "monomer MD worker active job capacity is full": "单体 MD worker 当前任务已满，请等待当前任务完成。",
  "Failed to fetch": "网络请求失败。"
};

const WORKER_STATUS_LABELS: Record<string, string> = {
  degraded: "降级",
  failed: "失败",
  ok: "正常",
  unreachable: "不可达",
  unknown: "未知"
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function numericValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function formatNumber(value: number, digits = 2) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(value);
}

function formatValue(value: unknown) {
  const numeric = numericValue(value);
  if (numeric != null) {
    return formatNumber(numeric, Math.abs(numeric) >= 100 ? 1 : 3);
  }
  if (typeof value === "boolean") {
    return value ? "是" : "否";
  }
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  return "--";
}

function translateResultMessage(message: string): string {
  return RESULT_MESSAGE_TRANSLATIONS[message] ?? message;
}

function translateRuntimeDetail(message: string): string {
  if (message.startsWith("BYTEFF2_DENSITY_DEMO_ENTRY does not exist:")) {
    return `BYTEFF2_DENSITY_DEMO_ENTRY 指向的入口文件不存在：${message.replace("BYTEFF2_DENSITY_DEMO_ENTRY does not exist:", "").trim()}`;
  }
  if (message.startsWith("ByteFF2 root does not exist:")) {
    return `ByteFF2 根目录不存在：${message.replace("ByteFF2 root does not exist:", "").trim()}`;
  }
  if (message.startsWith("BYTEFF2_PYTHON not found:")) {
    return `找不到 BYTEFF2_PYTHON：${message.replace("BYTEFF2_PYTHON not found:", "").trim()}`;
  }
  if (message === "gmx was not found on PATH") {
    return "PATH 中找不到 gmx 命令。";
  }
  if (message.startsWith("runtime import probe timed out after")) {
    return `运行环境导入检查超时：${message.replace("runtime import probe timed out after", "").trim()}`;
  }
  if (message.startsWith("gmx probe timed out after")) {
    return `gmx 检查超时：${message.replace("gmx probe timed out after", "").trim()}`;
  }
  return message;
}

function translateUiMessage(message: string): string {
  const trimmed = message.trim();
  const exact = UI_MESSAGE_TRANSLATIONS[trimmed];
  if (exact) {
    return exact;
  }
  if (trimmed.startsWith("invalid smiles:")) {
    return "SMILES 无法解析，请检查结构格式。";
  }
  if (trimmed.includes("single-molecule SMILES without attachment points")) {
    return "单体 MD 只接受普通单分子 SMILES，请不要输入带 * 的聚合物重复单元。";
  }
  if (trimmed.startsWith("MONOMER_MD_WORKER_BASE_URL")) {
    return "单体 MD worker 地址配置无效，请检查 MONOMER_MD_WORKER_BASE_URL。";
  }
  if (trimmed.startsWith("monomer MD worker health is ")) {
    const workerStatus = trimmed.replace("monomer MD worker health is ", "");
    return `单体 MD worker 健康状态异常：${WORKER_STATUS_LABELS[workerStatus] ?? workerStatus}`;
  }
  if (trimmed.startsWith("monomer MD worker health check failed:")) {
    return `单体 MD worker 健康检查失败：${translateUiMessage(trimmed.replace("monomer MD worker health check failed:", "").trim())}`;
  }
  if (trimmed.startsWith("monomer MD worker runtime is not ready:")) {
    return `单体 MD worker 运行环境尚未就绪：${translateRuntimeDetail(trimmed.replace("monomer MD worker runtime is not ready:", "").trim())}`;
  }
  if (trimmed.startsWith("monomer MD worker rejected the job:")) {
    return translateUiMessage(trimmed.replace("monomer MD worker rejected the job:", "").trim());
  }
  return trimmed || "单体 MD 请求失败。";
}

function normalizeSeries(series: MonomerMdSeries | undefined, valueKeys: string[]): PlotPoint[] {
  if (!series) {
    return [];
  }
  const rawPoints = Array.isArray(series) ? series : series.points;
  return rawPoints
    .map((point, index) => {
      const record = point as Record<string, unknown>;
      const timeNs = numericValue(record.time_ns);
      const x = numericValue(record.time_ps) ?? (timeNs == null ? null : timeNs * 1000) ?? numericValue(record.step) ?? numericValue(record.frame) ?? index;
      const y =
        numericValue(record.value) ??
        valueKeys.map((key) => numericValue(record[key])).find((value) => value != null) ??
        Object.entries(record)
          .filter(([key]) => !["time_ps", "time_ns", "step", "frame"].includes(key))
          .map(([, value]) => numericValue(value))
          .find((value) => value != null);
      return y == null || !Number.isFinite(x) ? null : { x, y };
    })
    .filter((point): point is PlotPoint => point !== null);
}

function polyline(points: PlotPoint[]) {
  const width = 320;
  const height = 120;
  const pad = 14;
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const xSpan = maxX - minX || 1;
  const ySpan = maxY - minY || 1;
  return points
    .map((point) => {
      const x = pad + ((point.x - minX) / xSpan) * (width - pad * 2);
      const y = height - pad - ((point.y - minY) / ySpan) * (height - pad * 2);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

function progressValue(status: MonomerMdJobStatus | null, progress: number | null | undefined) {
  const explicit = numericValue(progress);
  if (explicit != null) {
    return Math.max(0, Math.min(100, explicit <= 1 ? explicit * 100 : explicit));
  }
  if (status === "completed") return 100;
  if (status === "running") return 62;
  if (status === "submitted") return 24;
  if (status === "pending") return 10;
  if (status === "failed" || status === "cancelled") return 100;
  return 0;
}

function statusTone(status: MonomerMdJobStatus | null) {
  if (status === "completed") return "border-emerald-200 bg-emerald-50 text-emerald-700";
  if (status === "failed" || status === "cancelled") return "border-red-200 bg-red-50 text-red-700";
  if (status) return "border-sky-200 bg-sky-50 text-sky-700";
  return "border-slate-200 bg-slate-50 text-slate-600";
}

function summaryEntries(result: MonomerMdSimulationResult | null) {
  if (!result) return [];
  const seen = new Set<string>();
  const preferred = Object.keys(SUMMARY_LABELS)
    .filter((key) => key in result.summary)
    .map((key) => {
      seen.add(key);
      return [key, result.summary[key]] as const;
    });
  return [...preferred, ...Object.entries(result.summary).filter(([key]) => !seen.has(key))].slice(0, 8);
}

function normalizeArtifacts(artifacts: MonomerMdSimulationResult["artifacts"] | undefined): MonomerMdArtifact[] {
  if (!artifacts) return [];
  if (Array.isArray(artifacts)) return artifacts;
  return Object.entries(artifacts).map(([name, value]) => {
    if (isRecord(value)) {
      return { name, ...(value as MonomerMdArtifact) };
    }
    return { name, path: value == null ? null : String(value) };
  });
}

function trajectoryPoints(trajectory: MonomerMdTrajectoryPreview | null | undefined): MonomerMdTrajectoryPoint[] {
  return trajectory?.points ?? trajectory?.atoms ?? [];
}

function atomColor(point: MonomerMdTrajectoryPoint) {
  const label = (point.element ?? point.atom_type ?? "").toUpperCase();
  if (label.startsWith("O")) return "#ef4444";
  if (label.startsWith("N")) return "#2563eb";
  if (label.startsWith("H")) return "#94a3b8";
  if (label.startsWith("F") || label.startsWith("CL")) return "#10b981";
  return "#0f172a";
}

function projectAtom(point: MonomerMdTrajectoryPoint, points: MonomerMdTrajectoryPoint[]) {
  const xs = points.map((item) => item.x);
  const ys = points.map((item) => item.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const xSpan = maxX - minX || 1;
  const ySpan = maxY - minY || 1;
  return {
    x: 16 + ((point.x - minX) / xSpan) * 288,
    y: 164 - ((point.y - minY) / ySpan) * 148
  };
}

function StatusBadge({ status }: { status: MonomerMdJobStatus | null }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-semibold", statusTone(status))}>
      {status === "completed" ? <CheckCircle2 className="h-3.5 w-3.5" /> : null}
      {status === "failed" || status === "cancelled" ? <XCircle className="h-3.5 w-3.5" /> : null}
      {status === "pending" || status === "submitted" || status === "running" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
      {status ? STATUS_LABELS[status] : "未提交"}
    </span>
  );
}

function SeriesCard({ title, chartLabel, series, unit, color, valueKeys, isLoading }: { title: string; chartLabel: string; series: MonomerMdSeries | undefined; unit: string; color: string; valueKeys: string[]; isLoading: boolean }) {
  const points = normalizeSeries(series, valueKeys);
  const latest = points[points.length - 1];
  return (
    <section className="flex min-h-[230px] flex-col rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold uppercase text-slate-400">{title}</div>
          <div className="mt-1 text-sm font-semibold text-slate-950">{chartLabel}</div>
        </div>
        <div className="text-right">
          <div className="text-lg font-semibold tabular-nums text-slate-950">{latest ? formatNumber(latest.y, Math.abs(latest.y) >= 100 ? 1 : 3) : "--"}</div>
          <div className="text-[11px] text-slate-500">{unit}</div>
        </div>
      </div>
      <div className="mt-4 flex min-h-[126px] flex-1 items-center justify-center rounded-lg border border-slate-100 bg-slate-50/80">
        {points.length > 1 ? (
          <svg viewBox="0 0 320 120" role="img" aria-label={chartLabel} className="h-full w-full">
            <line x1="14" y1="28" x2="306" y2="28" stroke="#e2e8f0" strokeDasharray="4 5" />
            <line x1="14" y1="60" x2="306" y2="60" stroke="#e2e8f0" strokeDasharray="4 5" />
            <line x1="14" y1="92" x2="306" y2="92" stroke="#e2e8f0" strokeDasharray="4 5" />
            <polyline points={polyline(points)} fill="none" stroke={color} strokeLinecap="round" strokeLinejoin="round" strokeWidth="3" />
          </svg>
        ) : (
          <div className="px-4 text-center text-xs leading-5 text-slate-500">{isLoading ? "正在等待后端返回曲线数据。" : "提交一个 SMILES 后显示该曲线。"}</div>
        )}
      </div>
      <div className="mt-3 flex items-center justify-between gap-3 text-[11px] text-slate-500">
        <span>{points.length ? `${points.length} 个采样点` : "暂无数据"}</span>
        <span>{latest ? formatNumber(latest.x, 1) : "--"}</span>
      </div>
    </section>
  );
}

function SummaryPanel({ result }: { result: MonomerMdSimulationResult | null }) {
  const entries = summaryEntries(result);
  return (
    <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Gauge className="h-4 w-4 text-sky-600" />结果摘要</div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        {entries.length ? entries.map(([key, value]) => (
          <div key={key} className="min-h-[72px] rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
            <div className="truncate text-[11px] text-slate-500">{SUMMARY_LABELS[key] ?? key}</div>
            <div className="mt-1 break-words text-sm font-semibold text-slate-950">{formatValue(value)}</div>
          </div>
        )) : (
          <div className="col-span-full min-h-[72px] rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-5 text-sm text-slate-500">完成后将显示密度、温度、能量和采样摘要。</div>
        )}
      </div>
    </section>
  );
}

function MetricsPanel({ result }: { result: MonomerMdSimulationResult | null }) {
  const metrics = result?.metrics;
  const entries = metrics && isRecord(metrics)
    ? Object.entries(metrics).filter(([, value]) => ["string", "number", "boolean"].includes(typeof value)).slice(0, 10)
    : [];
  if (!entries.length) {
    return null;
  }
  return (
    <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><FileJson className="h-4 w-4 text-slate-600" />正式协议指标</div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
        {entries.map(([key, value]) => (
          <div key={key} className="min-h-[72px] rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
            <div className="truncate text-[11px] text-slate-500">{SUMMARY_LABELS[key] ?? key}</div>
            <div className="mt-1 break-words text-sm font-semibold text-slate-950">{formatValue(value)}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function TrajectoryCard({ trajectory }: { trajectory: MonomerMdTrajectoryPreview | null | undefined }) {
  const points = trajectoryPoints(trajectory);
  const visiblePoints = points.slice(0, 180);
  return (
    <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Atom className="h-4 w-4 text-teal-600" />轨迹预览</div>
        <span className="rounded-md bg-slate-100 px-2 py-1 text-[11px] text-slate-600">{trajectory?.stage_id ?? "预览"}</span>
      </div>
      <div className="mt-4 min-h-[220px] rounded-lg border border-slate-100 bg-slate-50">
        {visiblePoints.length ? (
          <svg viewBox="0 0 320 180" role="img" aria-label="轨迹投影" className="h-full min-h-[220px] w-full">
            <rect x="12" y="12" width="296" height="156" rx="10" fill="#ffffff" stroke="#e2e8f0" />
            {visiblePoints.map((point, index) => {
              const projected = projectAtom(point, visiblePoints);
              return <circle key={`${point.atom_id ?? index}-${index}`} cx={projected.x} cy={projected.y} r={point.element === "H" || point.atom_type === "H" ? 2 : 3} fill={atomColor(point)} opacity="0.76" />;
            })}
          </svg>
        ) : trajectory?.preview_url ? (
          <div className="flex min-h-[220px] flex-col items-center justify-center gap-2 px-4 text-center text-sm text-slate-600"><Database className="h-5 w-5 text-slate-400" /><a className="font-medium text-sky-700 hover:text-sky-800" href={trajectory.preview_url} target="_blank" rel="noreferrer">打开轨迹预览</a></div>
        ) : (
          <div className="flex min-h-[220px] items-center justify-center px-4 text-center text-sm text-slate-500">完成后将显示轨迹采样或预览文件。</div>
        )}
      </div>
      <div className="mt-3 grid gap-2 text-xs text-slate-600 sm:grid-cols-3">
        <div className="rounded-lg bg-slate-50 px-3 py-2"><span className="block text-[11px] text-slate-400">帧</span><span className="font-semibold text-slate-900">{trajectory?.frame_index ?? "--"}</span></div>
        <div className="rounded-lg bg-slate-50 px-3 py-2"><span className="block text-[11px] text-slate-400">时间</span><span className="font-semibold text-slate-900">{trajectory?.time_ps != null ? `${formatNumber(trajectory.time_ps, 1)} ps` : "--"}</span></div>
        <div className="rounded-lg bg-slate-50 px-3 py-2"><span className="block text-[11px] text-slate-400">采样点</span><span className="font-semibold text-slate-900">{trajectory?.sampled_points ?? (points.length || "--")}</span></div>
      </div>
    </section>
  );
}

function ArtifactsPanel({
  result,
  job,
  onDelete,
  deleteError
}: {
  result: MonomerMdSimulationResult | null;
  job: { status?: MonomerMdJobStatus; artifact_root?: string | null; artifact_deleted_at?: string | null; artifact_delete_message?: string | null } | null;
  onDelete: () => void;
  deleteError: string | null;
}) {
  const deleted = Boolean(job?.artifact_deleted_at);
  const artifacts = deleted ? [] : normalizeArtifacts(result?.artifacts);
  const canDelete = Boolean(job?.artifact_root) && !deleted && job?.status && !["pending", "submitted", "running"].includes(job.status);
  return (
    <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Database className="h-4 w-4 text-slate-600" />输出文件</div>
        {canDelete ? (
          <Button type="button" variant="outline" onClick={onDelete} className="h-8 rounded-md border-red-200 bg-white px-2.5 text-xs text-red-700 shadow-none hover:bg-red-50"><Trash2 className="mr-1.5 h-3.5 w-3.5" />删除</Button>
        ) : null}
      </div>
      {deleted ? <div className="mt-3 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">{job?.artifact_delete_message ?? "输出文件已删除，任务审计记录仍保留。"}</div> : null}
      {deleteError ? <div className="mt-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">{translateUiMessage(deleteError)}</div> : null}
      <div className="mt-3 space-y-2">
        {artifacts.length ? artifacts.map((artifact, index) => {
          const label = artifact.label ?? artifact.name ?? `artifact-${index + 1}`;
          const detail = artifact.path ?? artifact.kind ?? "artifact";
          return (
            <div key={`${label}-${index}`} className="flex min-h-[54px] items-center justify-between gap-3 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
              <div className="min-w-0"><div className="truncate text-sm font-medium text-slate-950">{label}</div><div className="truncate text-xs text-slate-500">{detail}</div></div>
              <div className="shrink-0 text-right text-xs text-slate-500">{artifact.url ? <a href={artifact.url} target="_blank" rel="noreferrer" className="font-medium text-sky-700 hover:text-sky-800">打开</a> : "--"}</div>
            </div>
          );
        }) : <div className="rounded-lg border border-dashed border-slate-200 bg-slate-50 px-3 py-5 text-sm text-slate-500">{deleted ? "输出文件已删除；任务审计记录和结果摘要仍保留。" : "完成后将显示轨迹、日志和曲线数据文件。"}</div>}
      </div>
    </section>
  );
}

function ResultNotice({ result, job, completedWithoutResult }: { result: MonomerMdSimulationResult | null; job: MonomerMdJobResponse | null; completedWithoutResult: boolean }) {
  if (completedWithoutResult) {
    return (
      <section className="rounded-[14px] border border-red-100 bg-red-50 px-4 py-3 text-sm leading-6 text-red-700">
        <span className="inline-flex items-start gap-2">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
          任务已完成，但后端没有返回结果数据。使用本次结果前，请先检查 worker 日志和输出文件路径。
        </span>
      </section>
    );
  }
  if (!result) {
    return null;
  }

  const messages = new Set<string>();
  const demoNotice = monomerMdDemoNotice(result, job);
  if (demoNotice) {
    messages.add(demoNotice);
  }
  for (const warning of result.warnings ?? []) {
    if (warning.trim() && !isGenericMonomerMdDemoWarning(warning)) {
      messages.add(translateResultMessage(warning.trim()));
    }
  }
  if (!messages.size) {
    return null;
  }

  return (
    <section className="rounded-[14px] border border-amber-100 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900">
      <div className="inline-flex items-start gap-2">
        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
        <div className="space-y-1">
          {[...messages].map((message) => (
            <div key={message}>{message}</div>
          ))}
        </div>
      </div>
    </section>
  );
}

export function MonomerMdSimulationPage({ onBackHome }: MonomerMdSimulationPageProps) {
  const simulation = useMonomerMdSimulation();
  const validationError = simulation.runMode === "demo" ? getMonomerMdSmilesValidationError(simulation.smiles) : null;
  const serviceUnavailable = simulation.serviceStatus?.enabled === false || simulation.serviceStatus?.available === false;
  const serviceBusy = simulation.serviceStatus?.busy === true;
  const serviceDraining = simulation.serviceStatus?.draining === true;
  const serviceCanSubmit = monomerMdServiceCanSubmit(
    simulation.serviceStatus,
    simulation.isStatusLoading,
    simulation.statusError
  );
  const protocolCatalog = simulation.protocolCatalog?.protocols ?? [];
  const selectedProtocolInfo = protocolCatalog.find((item) => item.protocol === simulation.selectedProtocol);
  const formalReady = simulation.runMode !== "formal" || selectedProtocolInfo?.runtime_ready === true;
  const formalUnavailable = simulation.runMode === "formal" && selectedProtocolInfo?.runtime_ready === false;
  const formalUnknown = simulation.runMode === "formal" && selectedProtocolInfo?.runtime_ready !== true && !formalUnavailable;
  const canSubmit = !simulation.isLoading
    && !validationError
    && !serviceUnavailable
    && serviceCanSubmit
    && formalReady;
  const currentStatus = simulation.job?.status ?? null;
  const progress = progressValue(currentStatus, simulation.job?.progress);
  const result = simulation.data;
  const completedWithoutResult = currentStatus === "completed" && !result;
  const currentModeLabel = simulation.runMode === "formal" ? "正式协议" : "DensityDemo";
  const resultRunMode = result?.run_mode ?? simulation.job?.run_mode ?? simulation.runMode;
  const hasSeries = Boolean(result?.density_series || result?.temperature_series || result?.energy_series);
  const showSeriesCards = !result || hasSeries || resultRunMode === "demo";

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (canSubmit) {
      void simulation.submit(simulation.smiles);
    }
  }

  return (
    <div className="min-h-full bg-[#f1f5f9] text-slate-950">
      <div className="mx-auto flex w-full max-w-[1480px] flex-col gap-4">
        <nav className="flex flex-col gap-3 rounded-[14px] border border-slate-200 bg-white px-4 py-3 shadow-sm md:flex-row md:items-center md:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <Button type="button" variant="outline" onClick={onBackHome} className="h-9 shrink-0 rounded-md border-slate-200 bg-white px-3 text-slate-700 shadow-none hover:border-slate-300 hover:bg-slate-50"><ArrowLeft className="mr-2 h-4 w-4" />返回</Button>
            <div className="min-w-0"><div className="text-[11px] font-semibold uppercase text-slate-400">单体 MD 工作台</div><div className="truncate text-base font-semibold text-slate-950">单体 MD 模拟</div></div>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className={cn("inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 font-medium", serviceUnavailable ? "border-red-200 bg-red-50 text-red-700" : "border-slate-200 bg-slate-50 text-slate-600")}>
              {simulation.isStatusLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Activity className="h-3.5 w-3.5" />}
              {simulation.isStatusLoading ? "正在检查计算服务" : simulation.statusError ? "状态检查失败" : serviceUnavailable ? "计算服务不可用" : serviceDraining ? "部署排空中" : serviceBusy ? "任务容量已满" : "计算服务可用"}
            </span>
            <Button type="button" variant="outline" onClick={() => void simulation.refreshStatus()} className="h-8 rounded-md border-slate-200 bg-white px-2.5 text-xs text-slate-600 shadow-none hover:bg-slate-50"><RotateCw className="mr-1.5 h-3.5 w-3.5" />刷新</Button>
          </div>
        </nav>

        <section className="grid gap-4 xl:grid-cols-[420px_minmax(0,1fr)]">
          <div className="flex min-w-0 flex-col gap-4">
            <form onSubmit={handleSubmit} className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
              <div className="flex items-center justify-between gap-3"><div><div className="text-[11px] font-semibold uppercase text-slate-400">输入</div><h1 className="mt-1 text-base font-semibold text-slate-950">ByteFF2 {currentModeLabel}</h1></div><span className="rounded-md bg-sky-50 px-2 py-1 text-xs font-medium text-sky-700">{simulation.runMode === "formal" ? simulation.selectedProtocol : "演示"}</span></div>
              <div className="mt-4 grid grid-cols-2 gap-2 rounded-lg border border-slate-200 bg-slate-50 p-1">
                <Button type="button" variant="outline" onClick={() => simulation.setRunMode("demo")} disabled={simulation.isLoading} className={cn("h-9 rounded-md border-transparent text-sm shadow-none", simulation.runMode === "demo" ? "bg-white text-slate-950 shadow-sm" : "bg-transparent text-slate-600 hover:bg-white/70")}>DensityDemo</Button>
                <Button type="button" variant="outline" onClick={() => simulation.setRunMode("formal")} disabled={simulation.isLoading} className={cn("h-9 rounded-md border-transparent text-sm shadow-none", simulation.runMode === "formal" ? "bg-white text-slate-950 shadow-sm" : "bg-transparent text-slate-600 hover:bg-white/70")}>正式模块</Button>
              </div>

              {simulation.runMode === "demo" ? (
                <>
                  <label className="mt-4 block space-y-2"><span className="text-xs font-medium text-slate-600">SMILES</span><Textarea value={simulation.smiles} onChange={(event) => simulation.setSmiles(event.target.value)} placeholder="示例：CCOC(=O)c1ccc(N)cc1" spellCheck={false} className="min-h-[96px] rounded-lg border-slate-200 bg-white font-mono text-[13px] leading-5 text-slate-900 shadow-none placeholder:text-slate-400 focus-visible:ring-sky-200" disabled={simulation.isLoading} /></label>
                  <div className="mt-2 min-h-[20px] text-xs text-slate-500">{validationError ? <span className="text-red-600">{validationError}</span> : "可输入任意普通单分子 SMILES；不要输入带 * 的聚合物重复单元。"}</div>
                  <div className="mt-2 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">第一阶段只运行 300 步演示。结果尚未达到平衡，不能作为真实物理密度结论。</div>
                </>
              ) : (
                <>
                  <div className="mt-4 grid gap-2 sm:grid-cols-2">
                    {FORMAL_PROTOCOLS.map((protocol) => {
                      const protocolInfo = protocolCatalog.find((item) => item.protocol === protocol);
                      const isSelected = simulation.selectedProtocol === protocol;
                      const isReady = protocolInfo?.runtime_ready;
                      return (
                        <Button key={protocol} type="button" variant="outline" onClick={() => { simulation.setSelectedProtocol(protocol); simulation.loadProtocolTemplate(protocol); }} disabled={simulation.isLoading} className={cn("h-auto min-h-[54px] justify-between rounded-lg border px-3 py-2 text-left shadow-none", isSelected ? "border-sky-300 bg-sky-50 text-sky-900" : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50")}>
                          <span className="font-semibold">{PROTOCOL_LABELS[protocol]}</span>
                          <span className={cn("ml-3 shrink-0 rounded-md px-1.5 py-0.5 text-[11px]", isReady ? "bg-emerald-50 text-emerald-700" : protocolInfo ? "bg-amber-50 text-amber-700" : "bg-slate-100 text-slate-500")}>{isReady ? "ready" : protocolInfo ? "blocked" : "unknown"}</span>
                        </Button>
                      );
                    })}
                  </div>
                  <div className="mt-4 flex items-center justify-between gap-3"><span className="text-xs font-medium text-slate-600">ByteFF2 config JSON</span><Button type="button" variant="outline" onClick={() => simulation.loadProtocolTemplate()} disabled={simulation.isLoading} className="h-8 rounded-md border-slate-200 bg-white px-2.5 text-xs text-slate-700 shadow-none hover:bg-slate-50"><FileJson className="mr-1.5 h-3.5 w-3.5" />载入模板</Button></div>
                  <Textarea value={simulation.configText} onChange={(event) => simulation.setConfigText(event.target.value)} spellCheck={false} className="mt-2 min-h-[300px] rounded-lg border-slate-200 bg-white font-mono text-xs leading-5 text-slate-900 shadow-none focus-visible:ring-sky-200" disabled={simulation.isLoading} />
                  {formalUnavailable ? <div className="mt-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">当前协议运行环境未就绪：{translateRuntimeDetail(String(selectedProtocolInfo?.runtime_error ?? "runtime not ready"))}</div> : null}
                  {formalUnknown ? <div className="mt-3 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">正在等待该协议的运行环境 readiness，确认前不可提交正式任务。</div> : null}
                  {simulation.protocolsError ? <div className="mt-3 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">协议列表不可用：{translateUiMessage(simulation.protocolsError)}</div> : null}
                </>
              )}
              <div className="mt-4 flex flex-wrap items-center gap-2"><Button type="submit" disabled={!canSubmit} className="h-10 rounded-md px-4 shadow-none disabled:opacity-[0.45]">{simulation.isLoading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}{simulation.isLoading ? "运行中" : simulation.runMode === "formal" ? "提交正式任务" : "提交演示"}</Button><Button type="button" variant="outline" onClick={simulation.reset} disabled={simulation.isLoading} className="h-10 rounded-md border-slate-200 bg-white px-4 text-slate-700 shadow-none hover:bg-slate-50">清空结果</Button></div>
              {serviceUnavailable ? <div className="mt-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">{simulation.serviceStatus?.message ? translateUiMessage(simulation.serviceStatus.message) : "后端报告单体 MD 服务当前不可用。"}</div> : null}
              {!serviceUnavailable && serviceDraining ? <div className="mt-3 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">单体 MD worker 正在等待现有任务完成并进行部署升级，升级完成后会自动恢复提交。</div> : null}
              {!serviceUnavailable && !serviceDraining && serviceBusy ? <div className="mt-3 rounded-lg border border-sky-100 bg-sky-50 px-3 py-2 text-xs leading-5 text-sky-800">当前已有单体 MD 任务运行，任务完成后会自动释放提交容量。</div> : null}
              {simulation.statusError ? <div className="mt-3 rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">状态接口不可用：{translateUiMessage(simulation.statusError)}</div> : null}
            </form>

            <section className="rounded-[14px] border border-slate-200 bg-white p-4 shadow-sm">
              <div className="flex items-center justify-between gap-3"><div className="flex items-center gap-2 text-sm font-semibold text-slate-950"><Timer className="h-4 w-4 text-slate-600" />任务状态</div><StatusBadge status={currentStatus} /></div>
              <div className="mt-4 space-y-3">
                <label className="block space-y-1.5"><span className="text-xs font-medium text-slate-500">任务 ID</span><Input value={simulation.job?.job_id ?? ""} readOnly placeholder="暂无任务" className="h-9 rounded-md border-slate-200 bg-slate-50 font-mono text-xs shadow-none" /></label>
                <div className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-1">
                  <div className="rounded-lg bg-slate-50 px-3 py-2"><span className="block text-[11px] text-slate-400">协议</span><span className="font-semibold text-slate-900">{simulation.job?.protocol ? PROTOCOL_LABELS[simulation.job.protocol] : "--"}</span></div>
                  <div className="rounded-lg bg-slate-50 px-3 py-2"><span className="block text-[11px] text-slate-400">GPU</span><span className="font-semibold text-slate-900">{simulation.job?.gpu_device ?? "--"}</span></div>
                </div>
                <div><div className="mb-1 flex items-center justify-between text-xs text-slate-500"><span>{currentStatus ? STATUS_LABELS[currentStatus] : "提交后开始轮询后端任务。"}</span><span className="tabular-nums">{formatNumber(progress, 0)}%</span></div><div className="h-2 overflow-hidden rounded-full bg-slate-100"><div className={cn("h-full rounded-full transition-all", currentStatus === "failed" || currentStatus === "cancelled" ? "bg-red-500" : "bg-sky-500")} style={{ width: `${progress}%` }} /></div></div>
                <div className="grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-1">{STATUS_STEPS.map((status) => { const active = currentStatus === status; const complete = STATUS_STEPS.indexOf(currentStatus ?? "pending") > STATUS_STEPS.indexOf(status) || currentStatus === "completed"; return <div key={status} className={cn("flex items-center justify-between rounded-lg border px-3 py-2", active ? "border-sky-200 bg-sky-50 text-sky-800" : complete ? "border-emerald-100 bg-emerald-50 text-emerald-800" : "border-slate-100 bg-slate-50 text-slate-500")}><span>{STATUS_LABELS[status]}</span>{complete ? <CheckCircle2 className="h-3.5 w-3.5" /> : active ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <span className="h-3.5 w-3.5 rounded-full border border-slate-200" />}</div>; })}</div>
                {simulation.error ? <div className="rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700"><span className="inline-flex items-start gap-2"><TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />{translateUiMessage(simulation.error)}</span></div> : null}
              </div>
            </section>
          </div>

          <div className="grid min-w-0 gap-4">
            <ResultNotice result={result} job={simulation.job} completedWithoutResult={completedWithoutResult} />
            <SummaryPanel result={result} />
            <MetricsPanel result={result} />
            {showSeriesCards ? (
              <div className="grid gap-4 lg:grid-cols-3">
                <SeriesCard title="密度" chartLabel="密度曲线" series={result?.density_series} unit="g/cm3" color="#0ea5e9" valueKeys={["density", "density_g_cm3", "rho"]} isLoading={simulation.isLoading} />
                <SeriesCard title="温度" chartLabel="温度曲线" series={result?.temperature_series} unit="K" color="#10b981" valueKeys={["temperature", "temperature_k", "temp"]} isLoading={simulation.isLoading} />
                <SeriesCard title="能量" chartLabel="能量曲线" series={result?.energy_series} unit="kcal/mol" color="#6366f1" valueKeys={["energy", "total_energy", "total_energy_kcal_mol", "potential_energy"]} isLoading={simulation.isLoading} />
              </div>
            ) : (
              <section className="rounded-[14px] border border-slate-200 bg-white p-4 text-sm text-slate-600 shadow-sm">正式协议结果已写入摘要和输出文件；该协议未返回前端曲线数据。</section>
            )}
            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]"><TrajectoryCard trajectory={result?.trajectory_preview} /><ArtifactsPanel result={result} job={simulation.job} onDelete={() => void simulation.deleteArtifacts()} deleteError={simulation.artifactDeleteError} /></div>
          </div>
        </section>
      </div>
    </div>
  );
}
