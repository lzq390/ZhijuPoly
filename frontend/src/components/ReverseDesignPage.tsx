import { useState, type ReactNode } from "react";
import {
  Activity,
  Atom,
  ArrowLeft,
  Bell,
  Box,
  Database,
  Grid2X2,
  Orbit,
  Search,
  Settings,
  SlidersHorizontal,
  Target,
  Timer,
  User
} from "lucide-react";
import { ReverseDesignResults } from "./ReverseDesignResults";
import { StructurePreview3D } from "./StructurePreview3D";
import { MissingStructurePanel } from "./StructureWorkbenchPage";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { useReverseDesign } from "../hooks/useReverseDesign";
import { cn } from "../lib/utils";
import type { KnowledgeNavigationRequest, ReverseDesignTgRequest, StructureWorkspaceContext } from "../types";

type ReverseDesignPageProps = {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
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
  scanned,
  className
}: {
  progress: number;
  statusLabel: string;
  matched: number;
  scanned: number;
  className?: string;
}) {
  return (
    <DashboardPanel className={cn("flex min-h-[420px] flex-col", className)}>
      <div className="flex flex-1 flex-col p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="font-heading text-lg font-semibold text-slate-950">搜索进度</h2>
            <p className="mt-1 text-xs text-slate-500">Tg 搜索任务状态</p>
          </div>
          <div className="rounded-2xl border border-violet-100 bg-white px-3 py-2 text-right shadow-[0_14px_32px_rgba(139,92,246,0.12)]">
            <div className="text-xs text-slate-500">总体进度</div>
            <div className="font-heading mt-1 text-2xl font-semibold text-violet-600">{progress}%</div>
          </div>
        </div>

        <div className="mt-5 h-2 overflow-hidden rounded-full bg-slate-100 shadow-inner">
          <div
            className="h-full rounded-full bg-[linear-gradient(90deg,#2563eb_0%,#8b5cf6_100%)] transition-all duration-500"
            style={{ width: `${progress}%` }}
          />
        </div>

        <div className="mt-5 grid gap-4">
          <div className="relative h-[132px] overflow-hidden rounded-[18px] border border-sky-100 bg-white px-3 py-3 shadow-[0_14px_34px_rgba(37,99,235,0.08)]">
            <div className="absolute inset-x-4 top-1/2 border-t border-dashed border-slate-200" />
            <div className="absolute inset-x-4 top-[28%] border-t border-dashed border-slate-200" />
            <svg viewBox="0 0 640 150" className="relative h-[118px] w-full overflow-visible" aria-hidden="true">
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

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1">
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

export function ReverseDesignPage({ structure, onEditStructure, onOpenKnowledge }: ReverseDesignPageProps) {
  const smiles = structure.smiles;
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
    smiles.trim().length > 0 &&
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
      detail: smiles.trim() ? "共享结构" : "等待结构",
      icon: <Atom className="h-4 w-4" />,
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
    return (await structure.getCurrentSmiles()).trim();
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

            <div id="reverse-structure" className="scroll-mt-28 grid min-w-0 gap-4 xl:grid-cols-[minmax(280px,0.82fr)_minmax(340px,0.95fr)_minmax(300px,0.8fr)] xl:items-stretch">
              <div className="grid min-w-0 gap-4 xl:h-full xl:grid-rows-[auto_minmax(0,1fr)]">
                <DashboardPanel id="reverse-smiles" className="scroll-mt-28 flex min-h-[170px] flex-col">
                  <div className="flex flex-1 flex-col p-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge className={smiles.trim() ? "border border-cyan-200 bg-cyan-50 text-cyan-800" : "bg-slate-100 text-slate-700"}>
                            {smiles.trim() ? "结构已就绪" : "未设置结构"}
                          </Badge>
                          <Badge className="border border-violet-200 bg-violet-50 text-violet-800">共享结构</Badge>
                        </div>
                        <h2 className="font-heading mt-3 text-lg font-semibold text-slate-950">SMILES</h2>
                      </div>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={onEditStructure}
                        className="min-h-[40px] min-w-[104px] border-sky-100 bg-white text-slate-700 shadow-[0_12px_28px_rgba(37,99,235,0.08)] hover:border-blue-200 hover:bg-blue-50"
                      >
                        <Atom className="mr-2 h-4 w-4" />
                        编辑
                      </Button>
                    </div>
                    <div className="mt-3 max-h-[96px] overflow-y-auto rounded-[18px] border border-sky-100 bg-sky-50/70 px-3 py-2 font-mono-ui text-xs leading-5 text-slate-900 shadow-[inset_0_1px_0_rgba(255,255,255,0.86)] [overflow-wrap:anywhere]">
                      {smiles.trim() || "请先在结构工作台绘制、导入或输入结构。"}
                    </div>
                  </div>
                </DashboardPanel>

                {smiles.trim() ? (
                  <DashboardPanel id="reverse-preview" className="scroll-mt-28 flex min-h-[230px] flex-col">
                    <div className="px-4 py-3">
                      <div className="flex items-center justify-between gap-4">
                        <div>
                          <h2 className="font-heading text-base font-semibold text-slate-950">3D 结构图</h2>
                          <p className="mt-0.5 text-xs text-slate-500">当前共享结构。</p>
                        </div>
                        <Orbit className="h-5 w-5 text-cyan-600" />
                      </div>
                    </div>
                    <div className="flex min-h-0">
                      <StructurePreview3D
                        smiles={smiles}
                        variant="bare"
                        className="min-h-0 w-full !flex-none"
                        contentClassName="min-h-0 w-full !flex-none"
                        previewClassName="h-[168px] !min-h-[168px] !flex-none xl:h-[210px] xl:!min-h-[210px]"
                        viewerClassName="scale-[0.74]"
                        visualStyle="polished-atoms"
                      />
                    </div>
                  </DashboardPanel>
                ) : (
                  <MissingStructurePanel
                    title="请先设置逆向设计结构"
                    description="Tg 逆向设计会读取结构工作台中的共享 SMILES。先绘制、导入或输入结构后，再回到这里设置目标 Tg。"
                    onEditStructure={onEditStructure}
                  />
                )}
              </div>

              <DashboardPanel id="reverse-controls" className="scroll-mt-28 flex min-h-[420px] flex-col">
                <div className="px-5 py-3.5">
                  <div className="flex items-center justify-between gap-4">
                    <div>
                      <h2 className="font-heading text-lg font-semibold text-slate-950">逆向设计控制</h2>
                      <p className="mt-1 text-xs text-slate-500">设置目标 Tg 与候选筛选范围。</p>
                    </div>
                    <SlidersHorizontal className="h-5 w-5 text-violet-600" />
                  </div>
                </div>
                <div className="flex flex-1 flex-col p-4 pt-0">
                  <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
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
                    <label className="space-y-1.5 sm:col-span-2 xl:col-span-1 2xl:col-span-2">
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

                  <div className="mt-4 grid grid-cols-3 gap-2 text-center">
                    <div className="rounded-2xl border border-sky-100 bg-sky-50/70 px-2 py-2">
                      <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">目标</div>
                      <div className="mt-1 truncate text-sm font-semibold text-slate-950">{formatTargetTg(reverseDesign.request.target_tg)}</div>
                    </div>
                    <div className="rounded-2xl border border-violet-100 bg-violet-50/60 px-2 py-2">
                      <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">阈值</div>
                      <div className="mt-1 text-sm font-semibold text-slate-950">{reverseDesign.request.similarity_threshold}</div>
                    </div>
                    <div className="rounded-2xl border border-cyan-100 bg-cyan-50/60 px-2 py-2">
                      <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">候选</div>
                      <div className="mt-1 text-sm font-semibold text-slate-950">{reverseDesign.request.candidate_size}</div>
                    </div>
                  </div>

                  <div className="mt-auto pt-5">
                    <Button
                      type="button"
                      className="min-h-[48px] w-full rounded-[16px] bg-blue-600 text-white shadow-[0_18px_46px_rgba(37,99,235,0.34),0_6px_16px_rgba(15,23,42,0.08)] hover:bg-blue-500"
                      onClick={handleSubmit}
                      disabled={!canSubmit}
                    >
                      <Search className="mr-2 h-4 w-4" />
                      {reverseDesign.isLoading ? "搜索中..." : "运行 Tg 搜索"}
                    </Button>
                    {reverseDesign.error ? (
                      <p className="mt-3 text-center text-xs leading-5 text-rose-600">
                        {reverseDesign.error}
                      </p>
                    ) : null}
                  </div>
                </div>
              </DashboardPanel>
              <SearchProgressCard
                progress={progress}
                statusLabel={statusLabel}
                scanned={scannedRows}
                matched={resultCount}
                className="scroll-mt-28"
              />
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
