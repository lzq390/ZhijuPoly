import { useState, type ReactNode } from "react";
import {
  Activity,
  ArrowLeft,
  Bell,
  Box,
  Database,
  Folder,
  Grid2X2,
  MoreHorizontal,
  Orbit,
  Search,
  Settings,
  SlidersHorizontal,
  Target,
  Timer,
  User
} from "lucide-react";
import { KetcherEditor } from "./KetcherEditor";
import { ReverseDesignResults } from "./ReverseDesignResults";
import { StructurePreview3D } from "./StructurePreview3D";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Textarea } from "./ui/textarea";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import { useKetcher } from "../hooks/useKetcher";
import { useReverseDesign } from "../hooks/useReverseDesign";
import { cn } from "../lib/utils";
import type { KnowledgeNavigationRequest, ReverseDesignTgRequest } from "../types";

type ReverseDesignPageProps = {
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
};

type ReverseDesignView = "workspace" | "results";

type ToolDirectoryItem = {
  label: string;
  detail: string;
  icon: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
  tone?: "default" | "action" | "accent";
};

function parseOptionalNumber(value: string) {
  if (!value.trim()) {
    return null;
  }

  const parsed = Number(value);
  return Number.isNaN(parsed) ? null : parsed;
}

function isDecimalInput(value: string) {
  return /^\d*\.?\d*$/.test(value);
}

function isIntegerInput(value: string) {
  return /^\d*$/.test(value);
}

function formatTargetTg(value: number | null) {
  return value === null || Number.isNaN(value) ? "待设置" : `${value} °C`;
}

function clampMetric(value: number, min = 0, max = 1) {
  return Math.min(max, Math.max(min, value));
}

function formatMetricValue(value: number) {
  return `${Math.round(clampMetric(value) * 100)}%`;
}

function PolymerReverseLogo({ className }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn("relative block overflow-hidden", className)}
    >
      <img
        src="/images/polymer-reverse-logo-icon.png"
        alt=""
        className="h-full w-full object-contain"
      />
    </span>
  );
}

function ModelPerformanceRadar({
  targetTg,
  similarityThreshold,
  candidateSize,
  resultCount,
  scannedRows,
  progress,
  averageSimilarityScore,
  averageTgDifference,
  candidatePoolSize,
  queryTimeMs,
  bestSimilarityScore
}: {
  targetTg: number | null;
  similarityThreshold: number;
  candidateSize: number;
  resultCount: number;
  scannedRows: number;
  progress: number;
  averageSimilarityScore: number | null;
  averageTgDifference: number | null;
  candidatePoolSize: number | null;
  queryTimeMs: number | null;
  bestSimilarityScore: number | null;
}) {
  const hasObservedSearch = progress > 0 || resultCount > 0 || scannedRows > 0;
  const tgFit =
    averageTgDifference === null
      ? targetTg === null
        ? 0.56
        : 0.78
      : clampMetric(1 - averageTgDifference / 80, 0.18, 1);
  const similarity = clampMetric(averageSimilarityScore ?? bestSimilarityScore ?? similarityThreshold ?? 0.68, 0.18, 1);
  const yieldScore = hasObservedSearch ? clampMetric(resultCount / Math.max(candidateSize, 1), 0.16, 1) : 0.72;
  const coverage =
    hasObservedSearch && candidatePoolSize && candidatePoolSize > 0
      ? clampMetric(scannedRows / candidatePoolSize, 0.18, 1)
      : hasObservedSearch
        ? clampMetric(progress / 100, 0.18, 1)
        : 0.66;
  const speed =
    queryTimeMs === null
      ? hasObservedSearch
        ? 0.72
        : 0.7
      : clampMetric(1 - queryTimeMs / 3200, 0.2, 1);
  const confidence = clampMetric(tgFit * 0.42 + similarity * 0.34 + yieldScore * 0.14 + coverage * 0.1, 0.18, 1);
  const benchmark = clampMetric(similarityThreshold * 0.52 + tgFit * 0.24 + coverage * 0.24, 0.22, 0.86);
  const metrics = [
    { label: "Tg 匹配", value: tgFit, color: "#a78bfa" },
    { label: "相似度", value: similarity, color: "#38bdf8" },
    { label: "命中率", value: yieldScore, color: "#22d3ee" },
    { label: "速度", value: speed, color: "#34d399" },
    { label: "覆盖率", value: coverage, color: "#2dd4bf" },
    { label: "置信度", value: confidence, color: "#c084fc" }
  ];
  const centerX = 180;
  const centerY = 142;
  const maxRadius = 72;
  const labelRadius = 110;
  const toPoint = (index: number, radius: number) => {
    const angle = (-90 + (360 / metrics.length) * index) * (Math.PI / 180);
    return {
      x: centerX + Math.cos(angle) * radius,
      y: centerY + Math.sin(angle) * radius
    };
  };
  const gridPolygons = [0.25, 0.5, 0.75, 1].map((level) =>
    metrics.map((_, index) => {
      const point = toPoint(index, maxRadius * level);
      return `${point.x},${point.y}`;
    })
  );
  const valuePoints = metrics
    .map((metric, index) => {
      const point = toPoint(index, maxRadius * metric.value);
      return `${point.x},${point.y}`;
    })
    .join(" ");
  const benchmarkPoints = metrics
    .map((_, index) => {
      const point = toPoint(index, maxRadius * benchmark);
      return `${point.x},${point.y}`;
    })
    .join(" ");
  const signatureScore = Math.round(confidence * 100);
  const fitDelta = averageTgDifference === null ? "--" : `${averageTgDifference.toFixed(1)} °C`;

  return (
    <div className="relative mx-auto mt-3 min-h-[292px] w-full max-w-[calc(100vw-48px)] overflow-hidden bg-white px-0 py-2">
      <div className="relative flex flex-wrap items-center justify-between gap-2 px-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-500">
        <span>性能矩阵</span>
        <span className="rounded-full border border-sky-100 bg-white px-2.5 py-1 text-sky-700 shadow-[0_8px_20px_rgba(14,165,233,0.1)]">
          评分 {signatureScore}%
        </span>
      </div>
      <svg viewBox="0 0 360 284" className="relative mt-1 h-[226px] w-full max-w-full overflow-visible sm:h-[238px]" role="img" aria-label="模型表现雷达图">
        <defs>
          <radialGradient id="performanceCoreGlow" cx="50%" cy="45%" r="64%">
            <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.14" />
            <stop offset="48%" stopColor="#8b5cf6" stopOpacity="0.06" />
            <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="performanceRadarFill" x1="94" x2="266" y1="54" y2="230" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stopColor="#8b5cf6" stopOpacity="0.26" />
            <stop offset="55%" stopColor="#38bdf8" stopOpacity="0.16" />
            <stop offset="100%" stopColor="#22d3ee" stopOpacity="0.12" />
          </linearGradient>
          <linearGradient id="performanceRadarStroke" x1="98" x2="262" y1="62" y2="222" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stopColor="#8b5cf6" />
            <stop offset="45%" stopColor="#3b82f6" />
            <stop offset="100%" stopColor="#06b6d4" />
          </linearGradient>
          <filter id="performanceRadarGlow" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>
        <circle cx={centerX} cy={centerY} r="118" fill="url(#performanceCoreGlow)" />
        <circle cx={centerX} cy={centerY} r="86" fill="none" stroke="#38bdf8" strokeDasharray="2 8" strokeOpacity="0.18" />
        <circle cx={centerX} cy={centerY} r="102" fill="none" stroke="#8b5cf6" strokeDasharray="18 12" strokeOpacity="0.14" />
        {gridPolygons.map((points, index) => (
          <polygon
            key={points.join(" ")}
            points={points.join(" ")}
            fill="none"
            stroke={index === gridPolygons.length - 1 ? "#93c5fd" : "#bae6fd"}
            strokeOpacity={index === gridPolygons.length - 1 ? 0.76 : 0.58}
            strokeWidth={index === gridPolygons.length - 1 ? 1.3 : 1}
          />
        ))}
        <polygon points={benchmarkPoints} fill="none" stroke="#64748b" strokeDasharray="5 5" strokeOpacity="0.28" strokeWidth="1.2" />
        {metrics.map((metric, index) => {
          const axisPoint = toPoint(index, maxRadius);
          const labelPoint = toPoint(index, labelRadius);
          const labelAnchor = Math.abs(labelPoint.x - centerX) < 6 ? "middle" : labelPoint.x > centerX ? "start" : "end";
          const valueOffset = labelPoint.y < centerY ? -14 : 15;
          return (
            <g key={metric.label}>
              <line x1={centerX} y1={centerY} x2={axisPoint.x} y2={axisPoint.y} stroke="#bfdbfe" strokeOpacity="0.74" strokeWidth="1" />
              <circle cx={axisPoint.x} cy={axisPoint.y} r="2" fill={metric.color} fillOpacity="0.85" />
              <text
                x={labelPoint.x}
                y={labelPoint.y}
                textAnchor={labelAnchor}
                dominantBaseline="middle"
                className="fill-slate-600 text-[10px] font-semibold"
              >
                {metric.label}
              </text>
              <text
                x={labelPoint.x}
                y={labelPoint.y + valueOffset}
                textAnchor={labelAnchor}
                dominantBaseline="middle"
                className="fill-slate-400 text-[9px] font-semibold"
              >
                {formatMetricValue(metric.value)}
              </text>
            </g>
          );
        })}
        <polygon points={valuePoints} fill="url(#performanceRadarFill)" stroke="url(#performanceRadarStroke)" strokeWidth="2.8" filter="url(#performanceRadarGlow)" />
        {metrics.map((metric, index) => {
          const point = toPoint(index, maxRadius * metric.value);
          return (
            <g key={`${metric.label}-point`} filter="url(#performanceRadarGlow)">
              <circle cx={point.x} cy={point.y} r="7" fill={metric.color} fillOpacity="0.16" />
              <circle cx={point.x} cy={point.y} r="4.1" fill="#ffffff" stroke={metric.color} strokeWidth="2.4" />
              <circle cx={point.x} cy={point.y} r="1.6" fill={metric.color} />
            </g>
          );
        })}
        <circle cx={centerX} cy={centerY} r="5.6" fill="#ffffff" stroke="#38bdf8" strokeOpacity="0.86" strokeWidth="1.8" />
        <circle cx={centerX} cy={centerY} r="2.4" fill="#3b82f6" />
      </svg>
      <div className="relative grid grid-cols-1 gap-2 px-2 text-center text-[10px] font-semibold text-slate-500 sm:grid-cols-3">
        <div className="rounded-full border border-sky-100 bg-white/80 px-2 py-1.5 shadow-[0_10px_24px_rgba(14,165,233,0.1)]">
          <span className="text-sky-700">Tg Δ</span> {fitDelta}
        </div>
        <div className="rounded-full border border-violet-100 bg-white/80 px-2 py-1.5 shadow-[0_10px_24px_rgba(139,92,246,0.09)]">
          <span className="text-violet-700">候选池</span> {candidatePoolSize ?? "--"}
        </div>
        <div className="rounded-full border border-teal-100 bg-white/80 px-2 py-1.5 shadow-[0_10px_24px_rgba(20,184,166,0.09)]">
          <span className="text-teal-700">命中</span> {resultCount}
        </div>
      </div>
    </div>
  );
}

function DashboardPanel({ children, className, id }: { children: ReactNode; className?: string; id?: string }) {
  return (
    <section
      id={id}
      className={cn(
        "relative overflow-hidden rounded-[24px] border border-sky-100 bg-white shadow-[0_22px_58px_rgba(37,99,235,0.12),0_6px_18px_rgba(15,23,42,0.05)] ring-1 ring-white/80 transition-all duration-300 hover:shadow-[0_26px_68px_rgba(37,99,235,0.16),0_8px_24px_rgba(15,23,42,0.06)]",
        className
      )}
    >
      <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-sky-200 to-transparent" />
      <div className="relative">{children}</div>
    </section>
  );
}

function ToolDirectoryButton({ item, compact = false }: { item: ToolDirectoryItem; compact?: boolean }) {
  const classes = cn(
    "group inline-flex items-center gap-3 border text-left transition-all duration-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300",
    compact
      ? "min-h-11 whitespace-nowrap rounded-2xl px-3 py-2 text-sm"
      : "w-full rounded-[18px] px-3.5 py-3",
    item.active
      ? "border-blue-200 bg-blue-50 text-blue-950 shadow-[0_14px_30px_rgba(37,99,235,0.12),inset_3px_0_0_rgba(37,99,235,0.86)]"
      : item.tone === "action"
        ? "border-cyan-200 bg-cyan-50 text-cyan-900 hover:border-cyan-300 hover:bg-cyan-100"
        : item.tone === "accent"
          ? "border-violet-200 bg-violet-50 text-violet-900 hover:border-violet-300 hover:bg-violet-100"
          : "border-transparent bg-transparent text-slate-600 hover:border-sky-100 hover:bg-white hover:text-slate-950 hover:shadow-[0_12px_28px_rgba(37,99,235,0.08)]",
    item.disabled ? "cursor-not-allowed opacity-45" : "hover:-translate-y-0.5"
  );

  return (
    <button type="button" className={classes} onClick={item.onClick} disabled={item.disabled}>
      <span
        className={cn(
          "flex h-9 w-9 flex-none items-center justify-center rounded-xl border border-sky-100 bg-white shadow-[0_10px_22px_rgba(37,99,235,0.08)]",
          item.active ? "text-blue-600" : "text-slate-600"
        )}
      >
        {item.icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-semibold">{item.label}</span>
        {!compact ? <span className="mt-1 block truncate text-xs text-slate-500">{item.detail}</span> : null}
      </span>
    </button>
  );
}

function MetricCard({
  icon,
  label,
  value,
  trend,
  accent = "blue"
}: {
  icon: ReactNode;
  label: string;
  value: string | number;
  trend: string;
  accent?: "blue" | "violet" | "cyan";
}) {
  const accentClass = {
    blue: "bg-blue-50 text-blue-600",
    violet: "bg-violet-50 text-violet-600",
    cyan: "bg-cyan-50 text-cyan-700"
  }[accent];

  return (
    <div className="relative min-h-[124px] overflow-hidden rounded-[22px] border border-sky-100 bg-white px-5 py-5 shadow-[0_18px_42px_rgba(37,99,235,0.1),0_6px_16px_rgba(15,23,42,0.045)] ring-1 ring-white/80">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-blue-100" />
      <div className="flex items-start gap-4">
        <div className={cn("flex h-14 w-14 flex-none items-center justify-center rounded-[18px]", accentClass)}>
          {icon}
        </div>
        <div className="min-w-0">
          <div className="text-sm font-semibold text-slate-600">{label}</div>
          <div className="font-heading mt-2 text-3xl font-semibold leading-none text-slate-950">{value}</div>
          <div className="mt-3 text-sm font-medium text-emerald-600">{trend}</div>
        </div>
      </div>
    </div>
  );
}

function SearchProgressCard({
  progress,
  statusLabel,
  matched,
  scanned
}: {
  progress: number;
  statusLabel: string;
  matched: number;
  scanned: number;
}) {
  return (
    <DashboardPanel>
      <div className="p-5 md:p-6">
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div>
            <h2 className="font-heading text-xl font-semibold text-slate-950">搜索进度</h2>
          </div>
          <div className="rounded-2xl border border-violet-100 bg-white px-4 py-3 shadow-[0_14px_32px_rgba(139,92,246,0.12)]">
            <div className="text-xs text-slate-500">总体进度</div>
            <div className="font-heading mt-1 text-3xl font-semibold text-violet-600">{progress}%</div>
          </div>
        </div>

        <div className="mt-6 grid gap-5 lg:grid-cols-[minmax(0,1fr)_190px]">
          <div className="relative min-h-[150px] overflow-hidden rounded-[18px] border border-sky-100 bg-white px-4 py-4 shadow-[0_14px_34px_rgba(37,99,235,0.08)]">
            <div className="absolute inset-x-4 top-1/2 border-t border-dashed border-slate-200" />
            <div className="absolute inset-x-4 top-[28%] border-t border-dashed border-slate-200" />
            <svg viewBox="0 0 640 150" className="relative h-[150px] w-full overflow-visible" aria-hidden="true">
              <polyline
                points="12,125 66,108 118,96 170,96 222,78 274,64 326,70 378,62 430,56 482,42 534,36 586,34 628,22"
                fill="none"
                stroke="#2563eb"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="5"
              />
              <circle cx="628" cy="22" r="8" fill="#ffffff" stroke="#a855f7" strokeWidth="5" />
              <path
                d="M12 125 L66 108 L118 96 L170 96 L222 78 L274 64 L326 70 L378 62 L430 56 L482 42 L534 36 L586 34 L628 22 L628 150 L12 150 Z"
                fill="#2563eb"
                opacity="0.08"
              />
            </svg>
          </div>

          <div className="grid content-center gap-3">
            <div className="rounded-2xl border border-sky-100 bg-white px-4 py-3 shadow-[0_12px_28px_rgba(37,99,235,0.07)]">
              <div className="text-xs text-slate-500">状态</div>
              <div className="mt-1 text-lg font-semibold text-slate-900">{statusLabel}</div>
            </div>
            <div className="rounded-2xl border border-sky-100 bg-white px-4 py-3 shadow-[0_12px_28px_rgba(37,99,235,0.07)]">
              <div className="text-xs text-slate-500">已扫描 / 已命中</div>
              <div className="mt-1 text-lg font-semibold text-slate-900">
                {scanned} / {matched}
              </div>
            </div>
          </div>
        </div>
      </div>
    </DashboardPanel>
  );
}

function SmilesSequencePanel({
  smiles,
  placeholder,
  onChange
}: {
  smiles: string;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  return (
    <DashboardPanel className="min-h-[130px]">
      <div className="px-4 py-2">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="font-heading text-sm font-semibold uppercase tracking-[0.08em] text-slate-950">
              聚合物序列
            </h2>
          </div>
          <MoreHorizontal className="h-4 w-4 text-slate-400" />
        </div>
      </div>
      <div className="p-3">
        <Textarea
          value={smiles}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          spellCheck={false}
          className="min-h-[68px] resize-none rounded-[14px] border-sky-200 bg-sky-50/80 px-3 py-2 font-mono-ui text-sm leading-6 text-slate-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.92),0_10px_24px_rgba(14,165,233,0.08)] placeholder:text-sky-700/45 selection:bg-sky-200/70 focus-visible:ring-sky-300"
        />
      </div>
    </DashboardPanel>
  );
}

export function ReverseDesignPage({ onOpenKnowledge }: ReverseDesignPageProps) {
  const { smiles, setSmiles, iframeRef, setIsReady } = useKetcher();
  const reverseDesign = useReverseDesign();
  const [activeView, setActiveView] = useState<ReverseDesignView>("workspace");

  function updateRequest(partial: Partial<ReverseDesignTgRequest>) {
    reverseDesign.setRequest({
      ...reverseDesign.request,
      ...partial
    });
  }

  function scrollToTop() {
    window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  function openWorkspace() {
    setActiveView("workspace");
    scrollToTop();
  }

  function openResults() {
    setActiveView("results");
    scrollToTop();
  }

  function goToSection(sectionId: string) {
    setActiveView("workspace");
    window.requestAnimationFrame(() => {
      document.getElementById(sectionId)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function handleTargetTgChange(value: string) {
    if (isDecimalInput(value)) {
      updateRequest({ target_tg: parseOptionalNumber(value) });
    }
  }

  function handleSimilarityThresholdChange(value: string) {
    if (!isDecimalInput(value)) {
      return;
    }

    updateRequest({ similarity_threshold: value.trim() && value !== "." ? Number(value) : 0 });
  }

  function handleCandidateSizeChange(value: string) {
    if (!isIntegerInput(value)) {
      return;
    }

    updateRequest({ candidate_size: value.trim() ? Number(value) : 0 });
  }

  const canSubmit =
    !reverseDesign.isLoading &&
    reverseDesign.request.target_tg !== null &&
    !Number.isNaN(reverseDesign.request.target_tg) &&
    reverseDesign.request.similarity_threshold >= 0 &&
    reverseDesign.request.similarity_threshold <= 1 &&
    reverseDesign.request.candidate_size >= 1 &&
    reverseDesign.request.candidate_size <= 200;

  const resultCount = reverseDesign.data?.total ?? reverseDesign.job?.matched_count ?? 0;
  const scannedRows = reverseDesign.job?.scanned_rows ?? 0;
  const progress = reverseDesign.data
    ? 100
    : reverseDesign.isLoading
      ? Math.max(18, Math.min(92, Math.round((resultCount / Math.max(reverseDesign.request.candidate_size, 1)) * 100)))
      : 0;
  const statusLabel = reverseDesign.isLoading
    ? "扫描中"
    : reverseDesign.error
      ? "失败"
      : reverseDesign.data
        ? "完成"
        : "空闲";
  const observedCandidates = reverseDesign.data?.results ?? reverseDesign.job?.result?.results ?? [];
  const averageSimilarityScore = observedCandidates.length
    ? observedCandidates.reduce((total, candidate) => total + candidate.similarity_score, 0) / observedCandidates.length
    : null;
  const averageTgDifference = observedCandidates.length
    ? observedCandidates.reduce((total, candidate) => total + Math.abs(candidate.tg_difference), 0) / observedCandidates.length
    : null;
  const candidatePoolSize = reverseDesign.data?.candidate_pool_size ?? reverseDesign.job?.result?.candidate_pool_size ?? null;
  const queryTimeMs = reverseDesign.data?.query_time_ms ?? reverseDesign.job?.result?.query_time_ms ?? null;
  const bestSimilarityScore =
    reverseDesign.job?.best_similarity_score ??
    (observedCandidates.length ? Math.max(...observedCandidates.map((candidate) => candidate.similarity_score)) : null);

  const toolItems: ToolDirectoryItem[] = [
    {
      label: "概览",
      detail: "仪表盘摘要",
      icon: <Grid2X2 className="h-4 w-4" />,
      onClick: () => goToSection("reverse-overview"),
      active: activeView === "workspace"
    },
    {
      label: "结构",
      detail: "结构画布",
      icon: <Folder className="h-4 w-4" />,
      onClick: () => goToSection("reverse-structure")
    },
    {
      label: "结果",
      detail: `${resultCount} 个候选`,
      icon: <Box className="h-4 w-4" />,
      onClick: openResults,
      active: activeView === "results",
      tone: "accent"
    },
    {
      label: "历史",
      detail: "即将开放",
      icon: <Database className="h-4 w-4" />,
      onClick: () => undefined,
      disabled: true
    }
  ];

  async function getCurrentSmilesForSearch() {
    const fallbackSmiles = smiles.trim();
    const ketcher = iframeRef.current?.contentWindow?.ketcher;
    if (!ketcher || typeof ketcher.getSmiles !== "function") {
      return fallbackSmiles;
    }

    try {
      const editorSmiles = (await ketcher.getSmiles()).trim();
      if (editorSmiles && editorSmiles !== fallbackSmiles) {
        setSmiles(editorSmiles);
        reverseDesign.setRequest({ ...reverseDesign.request, smiles: editorSmiles });
      }
      return editorSmiles || fallbackSmiles;
    } catch (error) {
      console.error("Failed to read SMILES from Ketcher before Tg search", error);
      throw new Error("无法读取当前编辑器结构，请重试或先点击“从编辑器同步”。");
    }
  }

  async function handleSubmit() {
    let currentSmiles = "";
    try {
      currentSmiles = await getCurrentSmilesForSearch();
    } catch (error) {
      reverseDesign.reportError(error instanceof Error ? error.message : "无法读取当前编辑器结构。");
      return;
    }

    await reverseDesign.submit({
      ...reverseDesign.request,
      smiles: currentSmiles
    });
  }

  return (
    <div className="relative -mx-4 -my-5 w-auto overflow-x-clip bg-[#f5fbff] text-slate-900 md:-mx-8 md:-my-8">
      <header className="relative z-20 flex min-h-[76px] flex-col gap-4 border-b border-sky-100 bg-white px-4 py-4 shadow-[0_12px_34px_rgba(37,99,235,0.06)] lg:grid lg:grid-cols-[minmax(220px,1fr)_minmax(320px,560px)_minmax(220px,1fr)] lg:items-center lg:px-8">
        <button type="button" onClick={scrollToTop} className="flex w-fit items-center gap-3 text-left">
          <span className="flex h-12 w-12 items-center justify-center">
            <PolymerReverseLogo className="h-10 w-10" />
          </span>
          <span>
            <span className="block font-heading text-xl font-semibold text-slate-950">Tg 逆向设计</span>
          </span>
        </button>

        <div className="flex h-12 w-full items-center gap-3 rounded-[16px] border border-sky-100 bg-white px-4 text-slate-500 shadow-[0_14px_34px_rgba(37,99,235,0.09),0_4px_12px_rgba(15,23,42,0.035)] transition-all duration-300 hover:-translate-y-0.5 hover:border-blue-200 hover:shadow-[0_18px_44px_rgba(37,99,235,0.14),0_6px_16px_rgba(15,23,42,0.04)] lg:justify-self-center">
          <Search className="h-5 w-5 flex-none text-slate-400" />
          <span className="truncate text-sm md:text-base">搜索结构、Tg 目标或候选记录...</span>
        </div>

        <div className="flex flex-wrap items-center gap-3 lg:justify-end">
          <button type="button" className="flex h-11 w-11 items-center justify-center rounded-full border border-sky-100 bg-white text-slate-600 shadow-[0_12px_28px_rgba(37,99,235,0.08)] transition-all duration-300 hover:-translate-y-0.5 hover:border-blue-200 hover:text-blue-600 hover:shadow-[0_16px_34px_rgba(37,99,235,0.14)]">
            <Bell className="h-5 w-5" />
          </button>
          <button type="button" className="flex h-11 w-11 items-center justify-center rounded-full border border-sky-100 bg-white text-slate-600 shadow-[0_12px_28px_rgba(37,99,235,0.08)] transition-all duration-300 hover:-translate-y-0.5 hover:border-violet-200 hover:text-violet-600 hover:shadow-[0_16px_34px_rgba(139,92,246,0.14)]">
            <Settings className="h-5 w-5" />
          </button>
          <div className="flex h-12 items-center gap-3 rounded-full border border-sky-100 bg-white pl-2 pr-4 shadow-[0_12px_28px_rgba(37,99,235,0.08)] transition-all duration-300 hover:-translate-y-0.5 hover:border-blue-200 hover:shadow-[0_16px_34px_rgba(37,99,235,0.12)]">
            <span className="flex h-9 w-9 items-center justify-center rounded-full border border-sky-100 bg-white text-slate-600 shadow-sm">
              <User className="h-5 w-5" />
            </span>
            <span className="text-sm font-semibold text-slate-800">用户</span>
          </div>
        </div>
      </header>

      <div className="relative z-10 overflow-x-clip bg-[#f7f9fc]">
        <nav className="border-b border-sky-100 bg-[#eaf6ff] px-4 py-3 md:px-6" aria-label="逆向设计页面导航">
          <div className="flex min-w-0 gap-2 overflow-x-auto pb-1">
            {toolItems.map((item) => (
              <ToolDirectoryButton key={item.label} item={item} compact />
            ))}
          </div>
        </nav>

        <main className="relative min-w-0 overflow-x-clip bg-[#f7f9fc] px-4 py-4 md:px-6 md:py-6">
          <div className={cn("relative z-10", activeView === "workspace" ? "space-y-4" : "hidden")}>
            <div id="reverse-overview" className="scroll-mt-28" />

            <div id="reverse-structure" className="scroll-mt-28 grid min-w-0 gap-4 2xl:grid-cols-[minmax(520px,0.92fr)_minmax(0,1fr)] 2xl:items-stretch">
              <KetcherEditor
                smiles={smiles}
                iframeRef={iframeRef}
                onReadyChange={setIsReady}
                presetStructure={{
                  label: "加载演示结构",
                  smiles: REVERSE_DESIGN_DEMO_SMILES
                }}
                layout="split"
                showSmilesPanel={false}
                showToolsBadge={false}
                eyebrow=""
                title="分子画布"
                className="min-w-0 2xl:h-full"
                frameClassName="h-full min-h-[445px] 2xl:min-h-[520px]"
                iframeClassName="h-[400px] 2xl:h-[475px]"
                onChange={(value) => {
                  setSmiles(value);
                  reverseDesign.setRequest({ ...reverseDesign.request, smiles: value });
                }}
              />

              <div className="grid min-w-0 gap-4 2xl:h-full 2xl:grid-rows-[auto_minmax(0,1fr)]">
                <SmilesSequencePanel
                  smiles={smiles}
                  placeholder="例如：*CC*、CCO，或用于相似匹配的其他 SMILES"
                  onChange={(value) => {
                    setSmiles(value);
                    reverseDesign.setRequest({ ...reverseDesign.request, smiles: value });
                  }}
                />

                <div className="grid min-w-0 gap-4 xl:grid-cols-2 2xl:h-full">
                  <DashboardPanel id="reverse-preview" className="scroll-mt-28 flex h-full flex-col">
                    <div className="px-5 py-3.5">
                      <div className="flex items-center justify-between gap-4">
                        <div>
                          <h2 className="font-heading text-lg font-semibold text-slate-950">3D 结构图</h2>
                        </div>
                        <Orbit className="h-5 w-5 text-cyan-600" />
                      </div>
                    </div>
                    <div className="flex min-h-0 flex-1">
                      <StructurePreview3D
                        smiles={smiles}
                        variant="bare"
                        className="min-h-0 flex-1"
                        contentClassName="min-h-0 flex-1"
                        previewClassName="min-h-[220px] xl:min-h-[240px] 2xl:min-h-[280px]"
                        viewerClassName="translate-y-5 2xl:translate-y-6"
                        visualStyle="polished-atoms"
                      />
                    </div>
                  </DashboardPanel>

                  <DashboardPanel id="reverse-controls" className="scroll-mt-28 flex h-full flex-col">
                    <div className="px-5 py-3.5">
                      <div className="flex items-center justify-between gap-4">
                        <div>
                          <h2 className="font-heading text-lg font-semibold text-slate-950">逆向设计控制</h2>
                        </div>
                        <SlidersHorizontal className="h-5 w-5 text-violet-600" />
                      </div>
                    </div>
                    <div className="flex flex-1 flex-col p-4">
                      <div className="grid gap-3 sm:grid-cols-2">
                        <label className="space-y-1.5">
                          <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500">目标 Tg (°C)</span>
                          <Input
                            type="text"
                            inputMode="decimal"
                            value={reverseDesign.request.target_tg ?? ""}
                            onChange={(event) => handleTargetTgChange(event.target.value)}
                            placeholder="500"
                            className="border-sky-100 bg-white text-slate-900 shadow-[0_10px_24px_rgba(37,99,235,0.06)] placeholder:text-slate-400"
                          />
                        </label>
                        <label className="space-y-1.5">
                          <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500">相似度阈值</span>
                          <Input
                            type="text"
                            inputMode="decimal"
                            value={reverseDesign.request.similarity_threshold}
                            onChange={(event) => handleSimilarityThresholdChange(event.target.value)}
                            className="border-sky-100 bg-white text-slate-900 shadow-[0_10px_24px_rgba(37,99,235,0.06)]"
                          />
                        </label>
                        <label className="space-y-1.5 sm:col-span-2">
                          <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500">候选数量</span>
                          <Input
                            type="text"
                            inputMode="numeric"
                            pattern="[0-9]*"
                            value={reverseDesign.request.candidate_size}
                            onChange={(event) => handleCandidateSizeChange(event.target.value)}
                            className="border-sky-100 bg-white text-slate-900 shadow-[0_10px_24px_rgba(37,99,235,0.06)]"
                          />
                        </label>
                      </div>

                      <div className="mt-8 flex min-h-[144px] flex-col items-center justify-center gap-2 pb-2 xl:min-h-[160px] 2xl:min-h-[176px]">
                        <Button
                          type="button"
                          className="min-h-[48px] w-full max-w-[220px] rounded-[16px] bg-blue-600 text-white shadow-[0_18px_46px_rgba(37,99,235,0.34),0_6px_16px_rgba(15,23,42,0.08)] hover:bg-blue-500"
                          onClick={handleSubmit}
                          disabled={!canSubmit}
                        >
                          <Search className="mr-2 h-4 w-4" />
                          {reverseDesign.isLoading ? "搜索中..." : "运行 Tg 搜索"}
                        </Button>
                        {reverseDesign.error ? (
                          <p className="w-full max-w-[260px] text-center text-xs leading-5 text-rose-600">
                            {reverseDesign.error}
                          </p>
                        ) : null}
                      </div>
                    </div>
                  </DashboardPanel>
                </div>
              </div>
            </div>

            <div className="grid gap-4 xl:grid-cols-[minmax(0,0.58fr)_minmax(320px,0.42fr)]">
              <SearchProgressCard
                progress={progress}
                statusLabel={statusLabel}
                scanned={scannedRows}
                matched={resultCount}
              />

              <DashboardPanel>
                <div className="p-5">
                  <h2 className="font-heading text-lg font-semibold text-slate-950">模型表现</h2>
                  <ModelPerformanceRadar
                    targetTg={reverseDesign.request.target_tg}
                    similarityThreshold={reverseDesign.request.similarity_threshold}
                    candidateSize={reverseDesign.request.candidate_size}
                    resultCount={resultCount}
                    scannedRows={scannedRows}
                    progress={progress}
                    averageSimilarityScore={averageSimilarityScore}
                    averageTgDifference={averageTgDifference}
                    candidatePoolSize={candidatePoolSize}
                    queryTimeMs={queryTimeMs}
                    bestSimilarityScore={bestSimilarityScore}
                  />
                </div>
              </DashboardPanel>
            </div>
          </div>

          <div className={cn("relative z-10", activeView === "results" ? "space-y-4" : "hidden")}>
            <DashboardPanel>
              <div className="p-5 md:p-6">
                <div className="flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge className="border border-cyan-200 bg-cyan-50 text-cyan-800">逆向设计</Badge>
                      <Badge className="border border-violet-200 bg-violet-50 text-violet-800">{statusLabel}</Badge>
                    </div>
                    <h1 className="font-heading mt-5 text-[2rem] font-semibold leading-tight text-slate-950 md:text-[3rem]">
                      Tg 逆向设计结果
                    </h1>
                    <p className="mt-3 max-w-3xl text-base leading-7 text-slate-600">
                      候选结果审阅保留在同一科学工作台中，并保持当前画布状态。
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-3 xl:justify-end">
                    <Button
                      type="button"
                      variant="outline"
                      onClick={openWorkspace}
                      className="min-h-[44px] min-w-[172px] border-sky-100 bg-white text-slate-700 shadow-[0_12px_28px_rgba(37,99,235,0.08)] hover:border-blue-200 hover:bg-blue-50"
                    >
                      <ArrowLeft className="mr-2 h-4 w-4" />
                      返回工作台
                    </Button>
                    <Button
                      type="button"
                      onClick={handleSubmit}
                      disabled={!canSubmit}
                      className="min-h-[44px] min-w-[162px] rounded-[16px] bg-blue-600 text-white shadow-[0_18px_46px_rgba(37,99,235,0.3)] hover:bg-blue-500"
                    >
                      <Search className="mr-2 h-4 w-4" />
                      {reverseDesign.isLoading ? "搜索中..." : "再次运行"}
                    </Button>
                  </div>
                </div>

                <div className="mt-6 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                  <MetricCard icon={<Target className="h-7 w-7" />} label="目标 Tg" value={formatTargetTg(reverseDesign.request.target_tg)} trend="当前查询" />
                  <MetricCard icon={<Database className="h-7 w-7" />} label="命中" value={resultCount} trend="候选记录" accent="cyan" />
                  <MetricCard icon={<Timer className="h-7 w-7" />} label="已扫描" value={scannedRows} trend="已检查 PI 行" accent="violet" />
                  <MetricCard icon={<Activity className="h-7 w-7" />} label="状态" value={statusLabel} trend="任务状态" accent="blue" />
                </div>
              </div>
            </DashboardPanel>

            <div className="rounded-[24px] border border-sky-100 bg-white p-3 shadow-[0_22px_58px_rgba(37,99,235,0.12),0_6px_18px_rgba(15,23,42,0.05)] ring-1 ring-white/80">
              <ReverseDesignResults
                data={reverseDesign.data}
                error={reverseDesign.error}
                isLoading={reverseDesign.isLoading}
                job={reverseDesign.job}
                targetCandidateSize={reverseDesign.request.candidate_size}
                onOpenKnowledge={onOpenKnowledge}
              />
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
