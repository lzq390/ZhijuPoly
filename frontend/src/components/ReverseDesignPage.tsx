import { useState, type ReactNode } from "react";
import {
  Activity,
  ArrowLeft,
  ArrowRight,
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
  Timer
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

function formatTargetTg(value: number | null) {
  return value === null || Number.isNaN(value) ? "Waiting" : `${value} °C`;
}

function PolymerReverseLogo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" aria-hidden="true" className={className}>
      <polygon points="32 6 55 19 55 45 32 58 9 45 9 19" fill="#ecfeff" stroke="#38bdf8" strokeWidth="2.4" />
      <polygon points="32 12 49 22 49 42 32 52 15 42 15 22" fill="#ffffff" stroke="#7dd3fc" strokeWidth="1.8" />
      <polygon points="32 19 43 25.5 43 38.5 32 45 21 38.5 21 25.5" fill="#f0f9ff" stroke="#0284c7" strokeWidth="1.4" />
      <path d="M32 20v24M22 26l20 12M42 26 22 38M27 29l10 6M37 29l-10 6M27 35l10 6" fill="none" stroke="#0891b2" strokeWidth="1.8" strokeLinecap="round" />
      {[
        [32, 20],
        [22, 26],
        [42, 26],
        [27, 29],
        [37, 29],
        [32, 35],
        [22, 38],
        [42, 38],
        [27, 41],
        [37, 41],
        [32, 44]
      ].map(([cx, cy]) => (
        <circle key={`${cx}-${cy}`} cx={cx} cy={cy} r={2.8} fill="#ffffff" stroke="#06b6d4" strokeWidth="1.6" />
      ))}
      <path d="M18 18h-4v4M50 18v4h-4M18 46h-4v-4M50 46h-4v4" fill="none" stroke="#38bdf8" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function DashboardPanel({ children, className, id }: { children: ReactNode; className?: string; id?: string }) {
  return (
    <section
      id={id}
      className={cn(
        "relative overflow-hidden rounded-[24px] border border-slate-200 bg-white shadow-[0_1px_3px_rgba(15,23,42,0.04)]",
        className
      )}
    >
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
      ? "border-blue-200 bg-blue-50 text-blue-950 shadow-[inset_3px_0_0_rgba(37,99,235,0.86)]"
      : item.tone === "action"
        ? "border-cyan-200 bg-cyan-50 text-cyan-900 hover:border-cyan-300 hover:bg-cyan-100"
        : item.tone === "accent"
          ? "border-violet-200 bg-violet-50 text-violet-900 hover:border-violet-300 hover:bg-violet-100"
          : "border-transparent bg-transparent text-slate-600 hover:border-slate-200 hover:bg-white hover:text-slate-950",
    item.disabled ? "cursor-not-allowed opacity-45" : "hover:-translate-y-0.5"
  );

  return (
    <button type="button" className={classes} onClick={item.onClick} disabled={item.disabled}>
      <span
        className={cn(
          "flex h-9 w-9 flex-none items-center justify-center rounded-xl border border-slate-200 bg-white shadow-sm",
          item.active ? "text-blue-600" : "text-slate-600"
        )}
      >
        {item.icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate font-semibold">{item.label}</span>
        {!compact ? <span className="mt-1 block truncate text-xs text-slate-500">{item.detail}</span> : null}
      </span>
      {!compact ? (
        <ArrowRight className="h-4 w-4 flex-none text-slate-400 transition group-hover:translate-x-0.5 group-hover:text-blue-600" />
      ) : null}
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
    <div className="relative min-h-[124px] overflow-hidden rounded-[22px] border border-slate-200 bg-white px-5 py-5 shadow-[0_1px_3px_rgba(15,23,42,0.04)]">
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
            <h2 className="font-heading text-xl font-semibold text-slate-950">Search Progress</h2>
            <p className="mt-1 text-sm text-slate-500">Live Tg screening status and candidate accumulation.</p>
          </div>
          <div className="rounded-2xl border border-violet-200 bg-violet-50 px-4 py-3">
            <div className="text-xs text-slate-500">Overall Progress</div>
            <div className="font-heading mt-1 text-3xl font-semibold text-violet-600">{progress}%</div>
          </div>
        </div>

        <div className="mt-6 grid gap-5 lg:grid-cols-[minmax(0,1fr)_190px]">
          <div className="relative min-h-[150px] overflow-hidden rounded-[18px] border border-slate-200 bg-white px-4 py-4">
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
            <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
              <div className="text-xs text-slate-500">Status</div>
              <div className="mt-1 text-lg font-semibold text-slate-900">{statusLabel}</div>
            </div>
            <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
              <div className="text-xs text-slate-500">Scanned / Matched</div>
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
    <DashboardPanel className="min-h-[150px]">
      <div className="border-b border-slate-200 px-4 py-2.5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="font-heading text-sm font-semibold uppercase tracking-[0.08em] text-slate-950">
              Polymer Sequence
            </h2>
            <div className="mt-1 text-[11px] font-medium text-teal-600">SMILES Sequence</div>
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
          className="min-h-[86px] resize-none rounded-[14px] border-slate-200 bg-white px-3 py-2 font-mono-ui text-sm leading-6 text-slate-900 shadow-none placeholder:text-slate-400 focus-visible:ring-cyan-400"
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
    ? "Scanning"
    : reverseDesign.error
      ? "Failed"
      : reverseDesign.data
        ? "Complete"
        : "Idle";
  const runMessage = reverseDesign.job
    ? `${reverseDesign.job.status} | scanned ${reverseDesign.job.scanned_rows} | matched ${reverseDesign.job.matched_count}`
    : canSubmit
      ? "Target and structure are ready."
      : "Enter a structure and a numeric target Tg.";

  const toolItems: ToolDirectoryItem[] = [
    {
      label: "Overview",
      detail: "Dashboard summary",
      icon: <Grid2X2 className="h-4 w-4" />,
      onClick: () => goToSection("reverse-overview"),
      active: activeView === "workspace"
    },
    {
      label: "Projects",
      detail: "Structure canvas",
      icon: <Folder className="h-4 w-4" />,
      onClick: () => goToSection("reverse-structure")
    },
    {
      label: "Results",
      detail: `${resultCount} candidates`,
      icon: <Box className="h-4 w-4" />,
      onClick: openResults,
      active: activeView === "results",
      tone: "accent"
    },
    {
      label: "History",
      detail: "Coming soon",
      icon: <Database className="h-4 w-4" />,
      onClick: () => undefined,
      disabled: true
    }
  ];

  async function handleSubmit() {
    openResults();
    await reverseDesign.submit({
      ...reverseDesign.request,
      smiles
    });
  }

  return (
    <div className="relative left-1/2 -my-5 w-screen -translate-x-1/2 overflow-hidden bg-white text-slate-900 md:-my-8">
      <header className="relative z-20 flex min-h-[76px] flex-col gap-4 border-b border-slate-200 bg-white px-4 py-4 lg:grid lg:grid-cols-[minmax(220px,1fr)_minmax(320px,560px)_minmax(220px,1fr)] lg:items-center lg:px-8">
        <button type="button" onClick={scrollToTop} className="flex w-fit items-center gap-3 text-left">
          <span className="flex h-12 w-12 items-center justify-center rounded-2xl border border-cyan-200 bg-white text-cyan-700">
            <PolymerReverseLogo className="h-10 w-10" />
          </span>
          <span>
            <span className="block font-heading text-xl font-semibold text-slate-950">Tg Reverse Design</span>
          </span>
        </button>

        <div className="flex h-12 w-full items-center gap-3 rounded-[16px] border border-slate-200 bg-white px-4 text-slate-500 shadow-[0_1px_3px_rgba(15,23,42,0.04)] lg:justify-self-center">
          <Search className="h-5 w-5 flex-none text-slate-400" />
          <span className="truncate text-sm md:text-base">Search structures, Tg targets, candidate records...</span>
        </div>

        <div className="flex flex-wrap items-center gap-3 lg:justify-end">
          <Badge className="border border-cyan-200 bg-cyan-50 text-cyan-800">{statusLabel}</Badge>
          <button type="button" className="flex h-11 w-11 items-center justify-center rounded-full border border-slate-200 bg-white text-slate-600">
            <Bell className="h-5 w-5" />
          </button>
          <button type="button" className="flex h-11 w-11 items-center justify-center rounded-full border border-slate-200 bg-white text-slate-600">
            <Settings className="h-5 w-5" />
          </button>
          <div className="flex h-12 items-center gap-3 rounded-full border border-slate-200 bg-white pl-2 pr-4">
            <span className="flex h-9 w-9 items-center justify-center rounded-full border border-violet-200 bg-violet-50 text-violet-700">
              <Orbit className="h-5 w-5" />
            </span>
            <span className="text-sm font-semibold text-slate-800">Dr. Nova</span>
          </div>
        </div>
      </header>

      <div className="relative z-10 grid lg:grid-cols-[268px_minmax(0,1fr)]">
        <aside className="border-b border-slate-200 bg-white px-4 py-4 lg:min-h-[calc(100vh-76px)] lg:border-b-0 lg:border-r lg:px-5 lg:py-8">
          <div className="flex gap-2 overflow-x-auto lg:hidden">
            {toolItems.map((item) => (
              <ToolDirectoryButton key={item.label} item={item} compact />
            ))}
          </div>

          <div className="hidden space-y-4 lg:block">
            {toolItems.map((item) => (
              <ToolDirectoryButton key={item.label} item={item} />
            ))}
          </div>
        </aside>

        <main className="min-w-0 px-4 py-4 md:px-6 md:py-6">
          <div className={activeView === "workspace" ? "space-y-4" : "hidden"}>
            <div id="reverse-overview" className="scroll-mt-28" />

            <div id="reverse-structure" className="scroll-mt-28 grid gap-4 xl:grid-cols-[minmax(500px,0.48fr)_minmax(0,0.52fr)] xl:items-stretch">
              <KetcherEditor
                smiles={smiles}
                iframeRef={iframeRef}
                onReadyChange={setIsReady}
                presetStructure={{
                  label: "Load Demo Structure",
                  smiles: REVERSE_DESIGN_DEMO_SMILES
                }}
                layout="split"
                showSmilesPanel={false}
                eyebrow="Interactive Polymer Model"
                title="Molecular Canvas"
                className="h-full"
                frameClassName="h-full min-h-[500px] 2xl:min-h-[560px]"
                iframeClassName="h-[455px] 2xl:h-[515px]"
                onChange={(value) => {
                  setSmiles(value);
                  reverseDesign.setRequest({ ...reverseDesign.request, smiles: value });
                }}
              />

              <div className="grid min-w-0 gap-4 xl:h-full xl:grid-rows-[auto_minmax(0,1fr)]">
                <SmilesSequencePanel
                  smiles={smiles}
                  placeholder="For example: *CC*, CCO, or another SMILES for similarity matching"
                  onChange={(value) => {
                    setSmiles(value);
                    reverseDesign.setRequest({ ...reverseDesign.request, smiles: value });
                  }}
                />

                <div className="grid gap-4 xl:h-full xl:grid-cols-2">
                  <DashboardPanel id="reverse-preview" className="scroll-mt-28 flex h-full flex-col">
                    <div className="border-b border-slate-200 px-5 py-4">
                      <div className="flex items-center justify-between gap-4">
                        <div>
                          <h2 className="font-heading text-lg font-semibold text-slate-950">3D Structure Map</h2>
                          <p className="mt-1 text-sm text-slate-500">Molecular conformation preview</p>
                        </div>
                        <Orbit className="h-5 w-5 text-cyan-600" />
                      </div>
                    </div>
                    <div className="flex min-h-0 flex-1 p-4">
                      <StructurePreview3D
                        smiles={smiles}
                        variant="bare"
                        className="min-h-0 flex-1"
                        contentClassName="min-h-0 flex-1"
                        previewClassName="min-h-[260px] xl:min-h-[280px] 2xl:min-h-[320px]"
                      />
                    </div>
                  </DashboardPanel>

                  <DashboardPanel id="reverse-controls" className="scroll-mt-28 flex h-full flex-col">
                    <div className="border-b border-slate-200 px-5 py-4">
                      <div className="flex items-center justify-between gap-4">
                        <div>
                          <h2 className="font-heading text-lg font-semibold text-slate-950">Reverse Controls</h2>
                          <p className="mt-1 text-sm text-slate-500">Tg and similarity search settings</p>
                        </div>
                        <SlidersHorizontal className="h-5 w-5 text-violet-600" />
                      </div>
                    </div>
                    <div className="grid flex-1 gap-3 p-4 sm:grid-cols-2">
                      <label className="space-y-1.5">
                        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500">Target Tg (°C)</span>
                        <Input
                          type="number"
                          value={reverseDesign.request.target_tg ?? ""}
                          onChange={(event) => updateRequest({ target_tg: parseOptionalNumber(event.target.value) })}
                          placeholder="500"
                          className="border-slate-200 bg-white text-slate-900 placeholder:text-slate-400"
                        />
                      </label>
                      <label className="space-y-1.5">
                        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500">Similarity Threshold</span>
                        <Input
                          type="number"
                          min={0}
                          max={1}
                          step={0.01}
                          value={reverseDesign.request.similarity_threshold}
                          onChange={(event) => updateRequest({ similarity_threshold: Number(event.target.value) })}
                          className="border-slate-200 bg-white text-slate-900"
                        />
                      </label>
                      <label className="space-y-1.5 sm:col-span-2">
                        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500">Candidate Size</span>
                        <Input
                          type="number"
                          min={1}
                          max={200}
                          step={1}
                          value={reverseDesign.request.candidate_size}
                          onChange={(event) => updateRequest({ candidate_size: Number(event.target.value) })}
                          className="border-slate-200 bg-white text-slate-900"
                        />
                      </label>

                      <div className="rounded-[18px] border border-blue-100 bg-blue-50 px-4 py-3 text-sm leading-6 text-slate-700 sm:col-span-2">
                        {runMessage}
                      </div>

                      <Button
                        type="button"
                        className="min-h-[50px] w-full rounded-[16px] bg-blue-600 text-white shadow-[0_18px_46px_rgba(37,99,235,0.34)] hover:bg-blue-500 sm:col-span-2"
                        onClick={handleSubmit}
                        disabled={!canSubmit}
                      >
                        <Search className="mr-2 h-4 w-4" />
                        {reverseDesign.isLoading ? "Searching..." : "Run Tg Search"}
                      </Button>
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
                  <h2 className="font-heading text-lg font-semibold text-slate-950">Model Performance</h2>
                  <div className="mt-5 flex min-h-[220px] items-center justify-center rounded-[20px] border border-slate-200 bg-white">
                    <div className="relative h-[190px] w-[190px]">
                      <div className="absolute inset-0 rounded-full border border-blue-200" />
                      <div className="absolute inset-[14%] rounded-full border border-violet-200" />
                      <div className="absolute inset-[28%] rounded-full border border-cyan-200" />
                      <div className="absolute left-1/2 top-0 h-full border-l border-slate-200" />
                      <div className="absolute inset-y-0 left-0 right-0 top-1/2 border-t border-slate-200" />
                      <div className="absolute inset-[17%] rounded-[36%] border border-violet-400 bg-violet-100 shadow-[0_0_34px_rgba(139,92,246,0.14)]" />
                      <div className="absolute left-1/2 top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-blue-500 shadow-[0_0_20px_rgba(59,130,246,0.45)]" />
                    </div>
                  </div>
                </div>
              </DashboardPanel>
            </div>
          </div>

          <div className={activeView === "results" ? "space-y-4" : "hidden"}>
            <DashboardPanel>
              <div className="p-5 md:p-6">
                <div className="flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge className="border border-cyan-200 bg-cyan-50 text-cyan-800">Reverse Design</Badge>
                      <Badge className="border border-violet-200 bg-violet-50 text-violet-800">{statusLabel}</Badge>
                    </div>
                    <h1 className="font-heading mt-5 text-[2rem] font-semibold leading-tight text-slate-950 md:text-[3rem]">
                      Tg Reverse Design Results
                    </h1>
                    <p className="mt-3 max-w-3xl text-base leading-7 text-slate-600">
                      Candidate review remains in the same scientific workbench while preserving the active canvas state.
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-3 xl:justify-end">
                    <Button
                      type="button"
                      variant="outline"
                      onClick={openWorkspace}
                      className="min-h-[44px] min-w-[172px] border-slate-200 bg-white text-slate-700 hover:border-blue-200 hover:bg-blue-50"
                    >
                      <ArrowLeft className="mr-2 h-4 w-4" />
                      Back to Workbench
                    </Button>
                    <Button
                      type="button"
                      onClick={handleSubmit}
                      disabled={!canSubmit}
                      className="min-h-[44px] min-w-[162px] rounded-[16px] bg-blue-600 text-white shadow-[0_18px_46px_rgba(37,99,235,0.3)] hover:bg-blue-500"
                    >
                      <Search className="mr-2 h-4 w-4" />
                      {reverseDesign.isLoading ? "Searching..." : "Run Again"}
                    </Button>
                  </div>
                </div>

                <div className="mt-6 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                  <MetricCard icon={<Target className="h-7 w-7" />} label="Target Tg" value={formatTargetTg(reverseDesign.request.target_tg)} trend="Active query" />
                  <MetricCard icon={<Database className="h-7 w-7" />} label="Matched" value={resultCount} trend="Candidate records" accent="cyan" />
                  <MetricCard icon={<Timer className="h-7 w-7" />} label="Scanned" value={scannedRows} trend="PI rows reviewed" accent="violet" />
                  <MetricCard icon={<Activity className="h-7 w-7" />} label="Status" value={statusLabel} trend="Job state" accent="blue" />
                </div>
              </div>
            </DashboardPanel>

            <div className="rounded-[24px] border border-slate-200 bg-white p-3 shadow-[0_1px_3px_rgba(15,23,42,0.04)]">
              <ReverseDesignResults
                data={reverseDesign.data}
                error={reverseDesign.error}
                isLoading={reverseDesign.isLoading}
                job={reverseDesign.job}
                onOpenKnowledge={onOpenKnowledge}
              />
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
