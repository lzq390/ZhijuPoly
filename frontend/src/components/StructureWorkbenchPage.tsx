import { useState, type FormEvent, type ReactNode } from "react";
import {
  ArrowLeft,
  Atom,
  Copy,
  Database,
  FlaskConical,
  LoaderCircle,
  Microscope,
  Network,
  Orbit,
  Route,
  Search,
  Sparkles,
  TriangleAlert,
  Upload
} from "lucide-react";
import { KetcherEditor } from "./KetcherEditor";
import { StructurePreview3D } from "./StructurePreview3D";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import { cn } from "../lib/utils";
import { predictMonomerPrecursors, standardizeSmiles } from "../services/api";
import type {
  MonomerRetrosynthesisResponse,
  MonomerRetrosynthesisTargetRole,
  StructureWorkspaceContext
} from "../types";

type StructureWorkbenchPageProps = {
  structure: StructureWorkspaceContext;
  onBackHome: () => void;
  onOpenModule: (moduleId: string) => void;
};

type WorkbenchAction = {
  id: string;
  label: string;
  detail: string;
  icon: ReactNode;
  onClick: () => void;
};

type WorkbenchTabId = "structure" | "retrosynthesis";

const TARGET_ROLE_OPTIONS: { value: MonomerRetrosynthesisTargetRole; label: string }[] = [
  { value: "auto", label: "自动识别" },
  { value: "diamine", label: "二胺" },
  { value: "dianhydride", label: "二酐" },
  { value: "other", label: "其他单体" }
];

const TARGET_ROLE_LABEL: Record<MonomerRetrosynthesisTargetRole, string> = {
  auto: "自动",
  diamine: "二胺",
  dianhydride: "二酐",
  other: "其他"
};

function clampInteger(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) {
    return min;
  }
  return Math.min(max, Math.max(min, Math.round(value)));
}

function formatModelScore(score: number | null): string {
  return score === null ? "未返回" : score.toFixed(3);
}

export function WorkbenchPanel({ children, className, id }: { children: ReactNode; className?: string; id?: string }) {
  return (
    <section
      id={id}
      className={cn(
        "overflow-hidden rounded-[24px] border border-sky-100 bg-white shadow-[0_22px_58px_rgba(37,99,235,0.12),0_6px_18px_rgba(15,23,42,0.05)] ring-1 ring-white/80",
        className
      )}
    >
      {children}
    </section>
  );
}

export function CurrentStructurePanel({
  structure,
  onEditStructure,
  className,
  compact = false
}: {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
  className?: string;
  compact?: boolean;
}) {
  const hasStructure = structure.smiles.trim().length > 0;

  return (
    <WorkbenchPanel className={className}>
      <div className={cn("flex flex-col gap-4 p-4 md:flex-row md:items-center md:justify-between", compact ? "md:p-4" : "md:p-5")}>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge className={hasStructure ? "border border-cyan-200 bg-cyan-50 text-cyan-800" : "bg-slate-100 text-slate-700"}>
              {hasStructure ? "结构已就绪" : "未设置结构"}
            </Badge>
            <Badge className="border border-violet-200 bg-violet-50 text-violet-800">共享结构</Badge>
          </div>
          <div className="mt-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Current SMILES</div>
          <div className="mt-1 max-w-full break-all font-mono-ui text-sm leading-6 text-slate-950">
            {hasStructure ? structure.smiles : "请先在结构工作台绘制、导入或输入结构。"}
          </div>
        </div>
        <Button
          type="button"
          variant="outline"
          onClick={onEditStructure}
          className="min-h-[44px] min-w-[168px] border-sky-100 bg-white text-slate-700 shadow-[0_12px_28px_rgba(37,99,235,0.08)] hover:border-blue-200 hover:bg-blue-50"
        >
          <Atom className="mr-2 h-4 w-4" />
          编辑结构
        </Button>
      </div>
    </WorkbenchPanel>
  );
}

export function MissingStructurePanel({
  title,
  description,
  onEditStructure
}: {
  title: string;
  description: string;
  onEditStructure: () => void;
}) {
  return (
    <WorkbenchPanel>
      <div className="flex min-h-[340px] flex-col items-center justify-center px-6 py-12 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-[22px] border border-sky-100 bg-sky-50 text-blue-600 shadow-[0_16px_36px_rgba(37,99,235,0.12)]">
          <Atom className="h-6 w-6" />
        </div>
        <h2 className="font-heading mt-6 text-2xl font-semibold text-slate-950">{title}</h2>
        <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-600">{description}</p>
        <Button
          type="button"
          onClick={onEditStructure}
          className="mt-7 min-h-[46px] min-w-[190px] rounded-[16px] bg-blue-600 text-white shadow-[0_18px_46px_rgba(37,99,235,0.32)] hover:bg-blue-500"
        >
          <Atom className="mr-2 h-4 w-4" />
          前往结构工作台
        </Button>
      </div>
    </WorkbenchPanel>
  );
}

export function StructureWorkbenchPage({ structure, onBackHome, onOpenModule }: StructureWorkbenchPageProps) {
  const [activeTab, setActiveTab] = useState<WorkbenchTabId>("structure");
  const [isTextDirty, setIsTextDirty] = useState(false);
  const [isStandardizingSmiles, setIsStandardizingSmiles] = useState(false);
  const [isLoadingTextIntoCanvas, setIsLoadingTextIntoCanvas] = useState(false);
  const [structureSyncError, setStructureSyncError] = useState<string | null>(null);
  const [structureSyncMessage, setStructureSyncMessage] = useState<string | null>(null);
  const [retroSmiles, setRetroSmiles] = useState("");
  const [retroTargetRole, setRetroTargetRole] = useState<MonomerRetrosynthesisTargetRole>("auto");
  const [retroReturnCount, setRetroReturnCount] = useState(5);
  const [retroBeamCount, setRetroBeamCount] = useState(5);
  const [retroData, setRetroData] = useState<MonomerRetrosynthesisResponse | null>(null);
  const [retroError, setRetroError] = useState<string | null>(null);
  const [isRetrosynthesizing, setIsRetrosynthesizing] = useState(false);
  const hasStructure = structure.smiles.trim().length > 0;

  async function standardizeWorkbenchSmiles(
    rawSmiles = structure.smiles,
    options: { markDirty?: boolean; showSuccess?: boolean } = {}
  ) {
    const nextStructure = rawSmiles.trim();
    if (!nextStructure) {
      setStructureSyncError("请先输入 SMILES，再进行 RDKit 标准化。");
      setStructureSyncMessage(null);
      return null;
    }

    setIsStandardizingSmiles(true);
    setStructureSyncError(null);
    setStructureSyncMessage(null);
    try {
      const result = await standardizeSmiles({ smiles: nextStructure });
      structure.setSmiles(result.standardized_smiles);
      if (options.markDirty !== undefined) {
        setIsTextDirty(options.markDirty);
      }
      if (options.showSuccess !== false) {
        setStructureSyncMessage(
          result.changed
            ? "已使用 RDKit 转换为 canonical SMILES。"
            : "当前 SMILES 已是 RDKit canonical 形式。"
        );
      }
      return result.standardized_smiles;
    } catch (error) {
      console.error("Failed to standardize workbench SMILES", error);
      setStructureSyncError(error instanceof Error ? error.message : "RDKit 无法标准化当前 SMILES。");
      return null;
    } finally {
      setIsStandardizingSmiles(false);
    }
  }

  async function openModuleWithSyncedStructure(moduleId: string) {
    setStructureSyncError(null);
    setStructureSyncMessage(null);
    let currentSmiles = structure.smiles.trim();
    if (!isTextDirty) {
      currentSmiles = (await structure.getCurrentSmiles()).trim();
    }
    const standardizedSmiles = await standardizeWorkbenchSmiles(currentSmiles, {
      markDirty: false,
      showSuccess: false
    });
    if (!standardizedSmiles) {
      return;
    }
    onOpenModule(moduleId);
  }

  async function loadSmilesTextIntoCanvas() {
    const standardizedSmiles = await standardizeWorkbenchSmiles(structure.smiles, {
      markDirty: true,
      showSuccess: false
    });
    if (!standardizedSmiles) {
      return;
    }

    const ketcher = structure.iframeRef.current?.contentWindow?.ketcher;
    if (!ketcher || typeof ketcher.setMolecule !== "function") {
      structure.setIsReady(false);
      setStructureSyncError("结构编辑器尚未就绪。");
      return;
    }

    setIsLoadingTextIntoCanvas(true);
    setStructureSyncError(null);
    setStructureSyncMessage(null);
    try {
      await ketcher.setMolecule(standardizedSmiles);
      await new Promise((resolve) => window.setTimeout(resolve, 80));
      const editorSmiles =
        typeof ketcher.getSmiles === "function"
          ? (await ketcher.getSmiles()).trim()
          : standardizedSmiles;
      const nextSmiles = (await standardizeWorkbenchSmiles(editorSmiles || standardizedSmiles, {
        markDirty: false,
        showSuccess: false
      })) ?? standardizedSmiles;
      structure.setSmiles(nextSmiles);
      setStructureSyncError(null);
      setIsTextDirty(false);
      structure.setIsReady(true);
      setStructureSyncMessage("已使用 RDKit 标准化并加载到画布。");
    } catch (error) {
      console.error("Failed to load workbench SMILES into Ketcher", error);
      setStructureSyncError(error instanceof Error ? error.message : "无法将 SMILES 加载到画布。");
    } finally {
      setIsLoadingTextIntoCanvas(false);
    }
  }

  function copyCurrentStructureToRetrosynthesis() {
    const currentSmiles = structure.smiles.trim();
    setActiveTab("retrosynthesis");
    setRetroError(null);
    if (!currentSmiles) {
      setRetroError("当前结构为空，请先在结构页签绘制、导入或输入一个二胺/二酐单体。");
      return;
    }
    setRetroSmiles(currentSmiles);
  }

  async function submitRetrosynthesis(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    const targetSmiles = retroSmiles.trim() || structure.smiles.trim();
    if (!targetSmiles) {
      setRetroError("请输入目标二胺、二酐或其他单体的 SMILES。");
      setRetroData(null);
      return;
    }

    const numReturnSequences = clampInteger(retroReturnCount, 1, 10);
    const numBeams = Math.max(clampInteger(retroBeamCount, 1, 20), numReturnSequences);
    setRetroReturnCount(numReturnSequences);
    setRetroBeamCount(numBeams);
    setIsRetrosynthesizing(true);
    setRetroError(null);
    try {
      const data = await predictMonomerPrecursors({
        smiles: targetSmiles,
        target_role: retroTargetRole,
        num_beams: numBeams,
        num_return_sequences: numReturnSequences,
        max_new_tokens: 128
      });
      setRetroData(data);
      setActiveTab("retrosynthesis");
    } catch (error) {
      console.error("Failed to run monomer retrosynthesis", error);
      setRetroError(error instanceof Error ? error.message : "单体上游反推失败。");
      setRetroData(null);
    } finally {
      setIsRetrosynthesizing(false);
    }
  }

  function copyText(value: string | null | undefined) {
    if (!value) {
      return;
    }
    void navigator.clipboard?.writeText(value);
  }

  const actions: WorkbenchAction[] = [
    {
      id: "databaseQuery",
      label: "数据库查询",
      detail: "检查当前结构是否已存在于数据表。",
      icon: <Search className="h-4 w-4" />,
      onClick: () => void openModuleWithSyncedStructure("databaseQuery")
    },
    {
      id: "explorer",
      label: "性能探索",
      detail: "运行相似匹配或性质预测。",
      icon: <Atom className="h-4 w-4" />,
      onClick: () => void openModuleWithSyncedStructure("explorer")
    },
    {
      id: "reverseDesign",
      label: "Tg 逆向设计",
      detail: "用当前结构约束 Tg 候选搜索。",
      icon: <Sparkles className="h-4 w-4" />,
      onClick: () => void openModuleWithSyncedStructure("reverseDesign")
    },
    {
      id: "conditionalGeneration",
      label: "条件聚合物生成",
      detail: "基于当前结构和 Tg 变化生成候选。",
      icon: <Microscope className="h-4 w-4" />,
      onClick: () => void openModuleWithSyncedStructure("conditionalGeneration")
    }
  ];

  const tabs: { id: WorkbenchTabId; label: string; icon: ReactNode }[] = [
    { id: "structure", label: "结构", icon: <Atom className="h-4 w-4" /> },
    { id: "retrosynthesis", label: "反推", icon: <Route className="h-4 w-4" /> }
  ];

  const currentRetroTarget = retroSmiles.trim() || structure.smiles.trim();
  const validCandidateCount = retroData?.candidates.filter((candidate) => candidate.valid_smiles).length ?? 0;

  return (
    <div className="relative -mx-4 -my-5 w-auto overflow-x-clip bg-[#f5fbff] text-slate-900 md:-mx-8 md:-my-8">
      <header className="relative z-20 flex min-h-[76px] flex-col gap-4 border-b border-sky-100 bg-white px-4 py-4 shadow-[0_12px_34px_rgba(37,99,235,0.06)] lg:grid lg:grid-cols-[minmax(220px,1fr)_minmax(320px,560px)_minmax(220px,1fr)] lg:items-center lg:px-8">
        <button type="button" onClick={onBackHome} className="flex w-fit items-center gap-3 text-left">
          <span className="flex h-12 w-12 items-center justify-center rounded-[18px] border border-sky-100 bg-sky-50 text-blue-600">
            <Atom className="h-6 w-6" />
          </span>
          <span>
            <span className="block font-heading text-xl font-semibold text-slate-950">结构工作台</span>
            <span className="mt-1 block text-xs font-medium text-slate-500">统一画板、3D 与 SMILES 输入</span>
          </span>
        </button>

        <div className="flex h-12 w-full items-center gap-3 rounded-[16px] border border-sky-100 bg-white px-4 text-slate-500 shadow-[0_14px_34px_rgba(37,99,235,0.09),0_4px_12px_rgba(15,23,42,0.035)] lg:justify-self-center">
          <Search className="h-5 w-5 flex-none text-slate-400" />
          <span className="truncate font-mono-ui text-sm md:text-base">
            {hasStructure ? structure.smiles : "绘制或输入结构后进入下游模块"}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-3 lg:justify-end">
          <Button
            type="button"
            variant="outline"
            onClick={onBackHome}
            className="min-h-[44px] border-sky-100 bg-white text-slate-700 shadow-[0_12px_28px_rgba(37,99,235,0.08)] hover:border-blue-200 hover:bg-blue-50"
          >
            <ArrowLeft className="mr-2 h-4 w-4" />
            Home
          </Button>
        </div>
      </header>

      <nav className="relative z-20 border-b border-sky-100 bg-[#eaf6ff] px-4 py-3 md:px-6">
        <div className="flex w-full gap-2 overflow-x-auto rounded-[22px] bg-sky-50/90 p-1 shadow-[inset_0_1px_0_rgba(255,255,255,0.92)] md:w-fit">
          {tabs.map((tab) => {
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  "inline-flex min-h-[46px] min-w-[112px] flex-none items-center justify-center gap-2 rounded-[18px] px-4 text-sm font-semibold transition",
                  isActive
                    ? "border border-blue-200 bg-white text-blue-700 shadow-[0_14px_30px_rgba(37,99,235,0.18)]"
                    : "border border-transparent text-slate-500 hover:bg-white/70 hover:text-slate-900"
                )}
                aria-pressed={isActive}
              >
                <span
                  className={cn(
                    "flex h-8 w-8 items-center justify-center rounded-[14px]",
                    isActive ? "bg-blue-50 text-blue-600" : "bg-white/70 text-slate-500"
                  )}
                >
                  {tab.icon}
                </span>
                {tab.label}
              </button>
            );
          })}
        </div>
      </nav>

      <div className="relative z-10 overflow-x-clip bg-[#f7f9fc]">
        <main className="relative min-w-0 overflow-x-clip bg-[#f7f9fc] px-4 py-4 md:px-6 md:py-6">
          {activeTab === "structure" ? (
          <div className="grid min-w-0 gap-4 2xl:grid-cols-[minmax(520px,0.92fr)_minmax(0,1fr)] 2xl:items-stretch">
            <KetcherEditor
              smiles={structure.smiles}
              iframeRef={structure.iframeRef}
              onReadyChange={structure.setIsReady}
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
                structure.setSmiles(value);
                setIsTextDirty(false);
                setStructureSyncError(null);
              }}
            />

            <div className="grid min-w-0 gap-4 2xl:h-full 2xl:grid-rows-[auto_minmax(0,1fr)]">
              <WorkbenchPanel id="structure-editor" className="min-h-[130px]">
                <div className="px-5 py-3.5">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <h2 className="font-heading text-lg font-semibold text-slate-950">SMILES 序列</h2>
                      <p className="mt-1 text-xs text-slate-500">下游模块统一读取这一份结构输入。</p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => void standardizeWorkbenchSmiles(structure.smiles, { markDirty: true })}
                        disabled={!structure.smiles.trim() || isStandardizingSmiles || isLoadingTextIntoCanvas}
                        className="min-h-[38px] min-w-[138px] border-sky-100 bg-white px-3 text-slate-700 shadow-[0_10px_24px_rgba(37,99,235,0.08)] hover:border-blue-200 hover:bg-blue-50"
                      >
                        {isStandardizingSmiles ? (
                          <LoaderCircle className="mr-2 h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Sparkles className="mr-2 h-3.5 w-3.5" />
                        )}
                        RDKit 标准化
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => void loadSmilesTextIntoCanvas()}
                        disabled={!structure.smiles.trim() || isLoadingTextIntoCanvas || isStandardizingSmiles}
                        className="min-h-[38px] min-w-[132px] border-sky-100 bg-white px-3 text-slate-700 shadow-[0_10px_24px_rgba(37,99,235,0.08)] hover:border-blue-200 hover:bg-blue-50"
                      >
                        {isLoadingTextIntoCanvas ? (
                          <LoaderCircle className="mr-2 h-3.5 w-3.5 animate-spin" />
                        ) : (
                          <Upload className="mr-2 h-3.5 w-3.5" />
                        )}
                        加载到画布
                      </Button>
                      <FlaskConical className="h-5 w-5 text-blue-600" />
                    </div>
                  </div>
                </div>
                <div className="p-4 pt-0">
                  <textarea
                    value={structure.smiles}
                    onChange={(event) => {
                      structure.setSmiles(event.target.value);
                      setIsTextDirty(true);
                      setStructureSyncError(null);
                      setStructureSyncMessage(null);
                    }}
                    placeholder="例如：*CC*、CCO，或用于相似匹配的其他 SMILES"
                    spellCheck={false}
                    className="min-h-[92px] w-full resize-none rounded-[14px] border border-sky-200 bg-sky-50/80 px-3 py-2 font-mono-ui text-sm leading-6 text-slate-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.92),0_10px_24px_rgba(14,165,233,0.08)] placeholder:text-sky-700/45 selection:bg-sky-200/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300"
                  />
                  {structureSyncError ? (
                    <div className="mt-3 rounded-2xl border border-rose-100 bg-rose-50 px-3 py-2 text-xs leading-5 text-rose-700">
                      {structureSyncError}
                    </div>
                  ) : null}
                  {structureSyncMessage ? (
                    <div className="mt-3 rounded-2xl border border-cyan-100 bg-cyan-50 px-3 py-2 text-xs leading-5 text-cyan-700">
                      {structureSyncMessage}
                    </div>
                  ) : null}
                  {isTextDirty ? (
                    <div className="mt-3 rounded-2xl border border-amber-100 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-700">
                      文本已更新。直接进入下游模块会使用当前 SMILES；如需同步画板，请点击“加载到画布”。
                    </div>
                  ) : null}
                </div>
              </WorkbenchPanel>

              <div className="grid min-w-0 gap-4 xl:grid-cols-2 2xl:h-full">
                <WorkbenchPanel id="structure-preview" className="scroll-mt-28 flex h-full flex-col">
                  <div className="px-5 py-3.5">
                    <div className="flex items-center justify-between gap-4">
                      <div>
                        <h2 className="font-heading text-lg font-semibold text-slate-950">3D 结构图</h2>
                        <p className="mt-1 text-xs text-slate-500">跟随当前 SMILES 实时刷新。</p>
                      </div>
                      <Orbit className="h-5 w-5 text-cyan-600" />
                    </div>
                  </div>
                  <div className="flex min-h-0 flex-1">
                    <StructurePreview3D
                      smiles={structure.smiles}
                      variant="bare"
                      className="min-h-0 flex-1"
                      contentClassName="min-h-0 flex-1"
                      previewClassName="min-h-[260px] xl:min-h-[320px] 2xl:min-h-[380px]"
                      viewerClassName="translate-y-5 2xl:translate-y-6"
                      visualStyle="polished-atoms"
                    />
                  </div>
                </WorkbenchPanel>

                <WorkbenchPanel id="structure-actions" className="scroll-mt-28 flex h-full flex-col">
                  <div className="px-5 py-3.5">
                    <div className="flex items-center justify-between gap-4">
                      <div>
                        <h2 className="font-heading text-lg font-semibold text-slate-950">进入功能模块</h2>
                        <p className="mt-1 text-xs text-slate-500">各模块将使用当前共享结构。</p>
                      </div>
                      <Database className="h-5 w-5 text-violet-600" />
                    </div>
                  </div>
                  <div className="grid flex-1 gap-3 p-4">
                    {actions.map((action) => (
                      <button
                        key={action.id}
                        type="button"
                        onClick={action.onClick}
                        className="flex min-h-[86px] items-center gap-4 rounded-[18px] border border-sky-100 bg-white px-4 py-3 text-left text-slate-900 shadow-[0_12px_28px_rgba(37,99,235,0.08)] transition hover:-translate-y-0.5 hover:border-blue-200 hover:bg-blue-50"
                      >
                        <span className="flex h-11 w-11 flex-none items-center justify-center rounded-[16px] bg-white text-blue-600 shadow-sm">
                          {action.icon}
                        </span>
                        <span className="min-w-0">
                          <span className="block font-heading text-base font-semibold">{action.label}</span>
                          <span className="mt-1 block text-xs leading-5 text-slate-500">{action.detail}</span>
                        </span>
                      </button>
                    ))}
                  </div>
                </WorkbenchPanel>
              </div>
            </div>
          </div>
          ) : null}

          {activeTab === "retrosynthesis" ? (
            <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(340px,0.62fr)_minmax(0,1fr)]">
              <WorkbenchPanel>
                <form onSubmit={(event) => void submitRetrosynthesis(event)} className="p-5">
                  <div className="flex items-center justify-between gap-4">
                    <div>
                      <h2 className="font-heading text-lg font-semibold text-slate-950">二胺/二酐上游反推</h2>
                      <p className="mt-1 text-xs leading-5 text-slate-500">
                        输入目标单体 SMILES，ReactionT5 会生成可能的更小前体组合。
                      </p>
                    </div>
                    <Route className="h-5 w-5 text-violet-600" />
                  </div>

                  <label className="mt-5 block text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
                    Target monomer SMILES
                  </label>
                  <textarea
                    value={retroSmiles}
                    onChange={(event) => {
                      setRetroSmiles(event.target.value);
                      setRetroError(null);
                    }}
                    placeholder="例如：Nc1ccc(N)cc1，或二酐单体 SMILES"
                    spellCheck={false}
                    className="mt-2 min-h-[132px] w-full resize-none rounded-[16px] border border-violet-100 bg-violet-50/70 px-3 py-2 font-mono-ui text-sm leading-6 text-slate-950 shadow-[inset_0_1px_0_rgba(255,255,255,0.92)] placeholder:text-violet-700/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-300"
                  />

                  <div className="mt-4 grid gap-3 sm:grid-cols-3">
                    <label className="block">
                      <span className="text-xs font-semibold text-slate-600">单体类型</span>
                      <select
                        value={retroTargetRole}
                        onChange={(event) => setRetroTargetRole(event.target.value as MonomerRetrosynthesisTargetRole)}
                        className="mt-2 h-11 w-full rounded-2xl border border-sky-100 bg-white px-3 text-sm font-semibold text-slate-800 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300"
                      >
                        {TARGET_ROLE_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="block">
                      <span className="text-xs font-semibold text-slate-600">候选数</span>
                      <input
                        type="number"
                        min={1}
                        max={10}
                        value={retroReturnCount}
                        onChange={(event) => setRetroReturnCount(clampInteger(Number(event.target.value), 1, 10))}
                        className="mt-2 h-11 w-full rounded-2xl border border-sky-100 bg-white px-3 text-sm font-semibold text-slate-800 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300"
                      />
                    </label>
                    <label className="block">
                      <span className="text-xs font-semibold text-slate-600">Beam</span>
                      <input
                        type="number"
                        min={1}
                        max={20}
                        value={retroBeamCount}
                        onChange={(event) => setRetroBeamCount(clampInteger(Number(event.target.value), 1, 20))}
                        className="mt-2 h-11 w-full rounded-2xl border border-sky-100 bg-white px-3 text-sm font-semibold text-slate-800 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-300"
                      />
                    </label>
                  </div>

                  {retroError ? (
                    <div className="mt-4 flex gap-3 rounded-2xl border border-rose-100 bg-rose-50 px-4 py-3 text-sm leading-6 text-rose-700">
                      <TriangleAlert className="mt-0.5 h-4 w-4 flex-none" />
                      <span>{retroError}</span>
                    </div>
                  ) : null}

                  <div className="mt-5 flex flex-col gap-3 sm:flex-row">
                    <Button
                      type="submit"
                      disabled={isRetrosynthesizing || !currentRetroTarget}
                      className="min-h-[46px] bg-violet-600 text-white shadow-[0_18px_42px_rgba(124,58,237,0.28)] hover:bg-violet-500"
                    >
                      {isRetrosynthesizing ? (
                        <LoaderCircle className="mr-2 h-4 w-4 animate-spin" />
                      ) : (
                        <Sparkles className="mr-2 h-4 w-4" />
                      )}
                      运行反推
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      onClick={copyCurrentStructureToRetrosynthesis}
                      className="min-h-[46px] border-sky-100 bg-white text-slate-700"
                    >
                      <Atom className="mr-2 h-4 w-4" />
                      使用当前结构
                    </Button>
                  </div>

                  <div className="mt-5 rounded-[18px] border border-sky-100 bg-sky-50/80 px-4 py-3 text-xs leading-5 text-slate-600">
                    首次运行会下载并加载 ReactionT5 权重；输出为模型候选，需要结合 RDKit 合法性和实验可行性继续筛选。
                  </div>
                </form>
              </WorkbenchPanel>

              <WorkbenchPanel>
                <div className="p-5">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                      <h2 className="font-heading text-lg font-semibold text-slate-950">反推结果</h2>
                      <p className="mt-1 text-xs leading-5 text-slate-500">
                        合法 SMILES、反应提示和模型分数会在这里汇总。
                      </p>
                    </div>
                    {retroData ? (
                      <Badge className="border border-violet-200 bg-violet-50 text-violet-800">
                        {retroData.total} candidates
                      </Badge>
                    ) : null}
                  </div>

                  {isRetrosynthesizing ? (
                    <div className="mt-5 flex min-h-[360px] flex-col items-center justify-center rounded-[24px] border border-dashed border-violet-100 bg-violet-50/60 px-6 text-center text-sm text-violet-800">
                      <LoaderCircle className="mb-4 h-7 w-7 animate-spin" />
                      正在生成上游反应物候选...
                    </div>
                  ) : retroData ? (
                    <div className="mt-5 space-y-4">
                      <div className="grid gap-3 sm:grid-cols-3">
                        <div className="rounded-[18px] border border-sky-100 bg-white px-4 py-3 shadow-sm">
                          <div className="text-xs font-semibold text-slate-500">识别类型</div>
                          <div className="mt-1 text-base font-semibold text-slate-950">
                            {TARGET_ROLE_LABEL[retroData.inferred_target_role]}
                          </div>
                        </div>
                        <div className="rounded-[18px] border border-sky-100 bg-white px-4 py-3 shadow-sm">
                          <div className="text-xs font-semibold text-slate-500">合法候选</div>
                          <div className="mt-1 text-base font-semibold text-slate-950">
                            {validCandidateCount}/{retroData.total}
                          </div>
                        </div>
                        <div className="rounded-[18px] border border-sky-100 bg-white px-4 py-3 shadow-sm">
                          <div className="text-xs font-semibold text-slate-500">设备</div>
                          <div className="mt-1 text-base font-semibold text-slate-950">{retroData.device}</div>
                        </div>
                      </div>

                      <div className="rounded-[18px] border border-sky-100 bg-sky-50/70 px-4 py-3">
                        <div className="flex items-center justify-between gap-3">
                          <div className="min-w-0">
                            <div className="text-xs font-semibold text-slate-500">Canonical target</div>
                            <div className="mt-1 break-all font-mono-ui text-sm leading-6 text-slate-950">
                              {retroData.canonical_smiles}
                            </div>
                          </div>
                          <button
                            type="button"
                            onClick={() => copyText(retroData.canonical_smiles)}
                            className="flex h-9 w-9 flex-none items-center justify-center rounded-[14px] text-slate-500 transition hover:bg-white hover:text-blue-600"
                            aria-label="复制目标 SMILES"
                          >
                            <Copy className="h-4 w-4" />
                          </button>
                        </div>
                      </div>

                      <div className="space-y-3">
                        {retroData.candidates.map((candidate) => (
                          <article
                            key={`${candidate.rank}-${candidate.reactants_smiles}`}
                            className="rounded-[20px] border border-sky-100 bg-white p-4 shadow-[0_12px_28px_rgba(37,99,235,0.08)]"
                          >
                            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                              <div className="min-w-0">
                                <div className="flex flex-wrap items-center gap-2">
                                  <Badge className="border border-slate-200 bg-slate-50 text-slate-700">
                                    Rank {candidate.rank}
                                  </Badge>
                                  <Badge
                                    className={
                                      candidate.valid_smiles
                                        ? "border border-emerald-200 bg-emerald-50 text-emerald-700"
                                        : "border border-rose-200 bg-rose-50 text-rose-700"
                                    }
                                  >
                                    {candidate.valid_smiles ? "合法 SMILES" : "需人工校验"}
                                  </Badge>
                                  {candidate.all_reactants_smaller_than_target !== null ? (
                                    <Badge className="border border-blue-200 bg-blue-50 text-blue-700">
                                      {candidate.all_reactants_smaller_than_target ? "更小前体" : "含较大前体"}
                                    </Badge>
                                  ) : null}
                                </div>
                                <h3 className="mt-3 font-heading text-base font-semibold text-slate-950">
                                  {candidate.reaction_hint}
                                </h3>
                              </div>
                              <div className="rounded-[16px] border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-600">
                                score {formatModelScore(candidate.model_score)}
                              </div>
                            </div>

                            <div className="mt-4 space-y-2">
                              {candidate.reactants.map((reactant, index) => (
                                <div
                                  key={`${candidate.rank}-${index}-${reactant.input_smiles}`}
                                  className="grid gap-2 rounded-[16px] border border-slate-100 bg-slate-50/80 px-3 py-2 sm:grid-cols-[minmax(0,1fr)_auto]"
                                >
                                  <div className="min-w-0">
                                    <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                                      Reactant {index + 1}
                                    </div>
                                    <div className="mt-1 break-all font-mono-ui text-sm leading-6 text-slate-950">
                                      {reactant.canonical_smiles ?? reactant.input_smiles}
                                    </div>
                                  </div>
                                  <div className="flex items-center gap-2 sm:justify-end">
                                    <Badge className="border border-white bg-white text-slate-600">
                                      {reactant.heavy_atom_count ?? "-"} heavy atoms
                                    </Badge>
                                    <button
                                      type="button"
                                      onClick={() => copyText(reactant.canonical_smiles ?? reactant.input_smiles)}
                                      className="flex h-9 w-9 items-center justify-center rounded-[14px] text-slate-500 transition hover:bg-white hover:text-blue-600"
                                      aria-label="复制反应物 SMILES"
                                    >
                                      <Copy className="h-4 w-4" />
                                    </button>
                                  </div>
                                </div>
                              ))}
                            </div>
                          </article>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <div className="mt-5 flex min-h-[360px] flex-col items-center justify-center rounded-[24px] border border-dashed border-sky-100 bg-sky-50/70 px-6 text-center">
                      <Network className="h-8 w-8 text-slate-400" />
                      <h3 className="mt-4 font-heading text-lg font-semibold text-slate-950">等待反推</h3>
                      <p className="mt-2 max-w-md text-sm leading-6 text-slate-500">
                        输入二胺、二酐或其他目标单体后，候选上游反应物会显示在这里。
                      </p>
                    </div>
                  )}
                </div>
              </WorkbenchPanel>
            </div>
          ) : null}

        </main>
      </div>
    </div>
  );
}
