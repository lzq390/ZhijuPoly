import {
  ArrowLeft,
  ArrowUp,
  Atom,
  Box,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Copy,
  Database,
  Eraser,
  FlaskConical,
  Grid2X2,
  ImagePlus,
  LoaderCircle,
  MessageSquareText,
  Microscope,
  Network,
  Orbit,
  Plus,
  RefreshCcw,
  Route,
  Search,
  SlidersHorizontal,
  Sparkles,
  TriangleAlert,
  X,
  type LucideIcon
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode
} from "react";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import { cn } from "../lib/utils";
import { predictMonomerPrecursors } from "../services/api";
import type {
  MonomerRetrosynthesisResponse,
  MonomerRetrosynthesisTargetRole,
  StructureWorkspaceContext
} from "../types";
import "../styles/polymer-desktop.css";
import "../styles/reverse-design.css";
import "../styles/structure-workbench.css";
import { StructurePreview3D } from "./StructurePreview3D";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";

type StructureWorkbenchPageProps = {
  structure: StructureWorkspaceContext;
  onBackHome: () => void;
  onOpenModule: (moduleId: string) => void;
};

type ModuleRelationship = "direct" | "shared" | "optional" | "local";
type OpenWorkbenchPanel = "modules" | "assistant" | null;
type ModulePanelView = "grid" | "retrosynthesis";

type WorkbenchModule = {
  id: string;
  name: string;
  shortName: string;
  icon: LucideIcon;
  relationship: ModuleRelationship;
  isBuiltIn: boolean;
};

const DEFAULT_RETROSYNTHESIS_MONOMER_SMILES = "C=C(C)C(=O)OC";
const DRAWER_MIN_WIDTH = 320;
const DRAWER_MAX_WIDTH = 560;

const TARGET_ROLE_OPTIONS: { value: MonomerRetrosynthesisTargetRole; label: string }[] = [
  { value: "auto", label: "自动识别" },
  { value: "other", label: "通用单体" },
  { value: "diamine", label: "二胺提示" },
  { value: "dianhydride", label: "二酐提示" }
];

const TARGET_ROLE_LABEL: Record<MonomerRetrosynthesisTargetRole, string> = {
  auto: "自动",
  diamine: "二胺提示",
  dianhydride: "二酐提示",
  other: "通用单体"
};

const WORKBENCH_MODULES: WorkbenchModule[] = [
  {
    id: "databaseQuery",
    name: "数据库查询",
    shortName: "数据库查询",
    icon: Database,
    relationship: "direct",
    isBuiltIn: false
  },
  {
    id: "explorer",
    name: "聚合物性能探索",
    shortName: "性能探索",
    icon: Atom,
    relationship: "direct",
    isBuiltIn: false
  },
  {
    id: "monomerDft",
    name: "单体 DFT（AIMNet2）",
    shortName: "单体 DFT",
    icon: Orbit,
    relationship: "shared",
    isBuiltIn: false
  },
  {
    id: "monomerPolymerization",
    name: "单体正向聚合",
    shortName: "正向聚合",
    icon: FlaskConical,
    relationship: "shared",
    isBuiltIn: false
  },
  {
    id: "reverseDesign",
    name: "Tg 逆向设计",
    shortName: "Tg 逆向",
    icon: Sparkles,
    relationship: "direct",
    isBuiltIn: false
  },
  {
    id: "conditionalGeneration",
    name: "条件聚合物生成",
    shortName: "条件生成",
    icon: Microscope,
    relationship: "shared",
    isBuiltIn: false
  },
  {
    id: "polytaoGeneration",
    name: "PolyTAO 生成",
    shortName: "PolyTAO",
    icon: Sparkles,
    relationship: "optional",
    isBuiltIn: false
  },
  {
    id: "retrosynthesis",
    name: "单体逆合成反推",
    shortName: "单体反推",
    icon: Route,
    relationship: "local",
    isBuiltIn: true
  }
];

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

export function WorkbenchPanel({
  children,
  className,
  id
}: {
  children: ReactNode;
  className?: string;
  id?: string;
}) {
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
      <div
        className={cn(
          "flex flex-col gap-4 p-4 md:flex-row md:items-center md:justify-between",
          compact ? "md:p-4" : "md:p-5"
        )}
      >
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge
              className={
                hasStructure
                  ? "border border-cyan-200 bg-cyan-50 text-cyan-800"
                  : "bg-slate-100 text-slate-700"
              }
            >
              {hasStructure ? "结构已就绪" : "未设置结构"}
            </Badge>
            <Badge className="border border-violet-200 bg-violet-50 text-violet-800">
              共享结构
            </Badge>
          </div>
          <div className="mt-3 text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">
            Current SMILES
          </div>
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

export function StructureWorkbenchPage({
  structure,
  onOpenModule
}: StructureWorkbenchPageProps) {
  const [openPanel, setOpenPanel] = useState<OpenWorkbenchPanel>(null);
  const [modulePanelView, setModulePanelView] = useState<ModulePanelView>("grid");
  const [selectedModuleName, setSelectedModuleName] = useState("尚未选择任务");
  const [openingModuleId, setOpeningModuleId] = useState<string | null>(null);
  const [assistantInput, setAssistantInput] = useState("");
  const [assistantNotice, setAssistantNotice] = useState<string | null>(null);
  const [retroSmiles, setRetroSmiles] = useState(DEFAULT_RETROSYNTHESIS_MONOMER_SMILES);
  const [retroTargetRole, setRetroTargetRole] =
    useState<MonomerRetrosynthesisTargetRole>("auto");
  const [retroReturnCount, setRetroReturnCount] = useState("5");
  const [showRetroValidation, setShowRetroValidation] = useState(false);
  const [retroData, setRetroData] = useState<MonomerRetrosynthesisResponse | null>(null);
  const [selectedRetroCandidateIndex, setSelectedRetroCandidateIndex] = useState(0);
  const [retroError, setRetroError] = useState<string | null>(null);
  const [isRetrosynthesizing, setIsRetrosynthesizing] = useState(false);
  const [hasRetroRun, setHasRetroRun] = useState(false);
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const modulePanelRef = useRef<HTMLElement | null>(null);
  const assistantPanelRef = useRef<HTMLElement | null>(null);
  const moduleButtonRef = useRef<HTMLButtonElement | null>(null);
  const assistantButtonRef = useRef<HTMLButtonElement | null>(null);
  const resizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const handleStructureChanged = useCallback(() => {
    setAssistantNotice(null);
  }, []);

  const canvas = useTgStructureCanvas({
    structure,
    onStructureChanged: handleStructureChanged
  });

  const parsedRetroReturnCount = Number(retroReturnCount);
  const retroTargetValidation = retroSmiles.trim() ? null : "请输入目标单体的 SMILES。";
  const retroCountValidation =
    retroReturnCount.trim() &&
    Number.isInteger(parsedRetroReturnCount) &&
    parsedRetroReturnCount >= 1 &&
    parsedRetroReturnCount <= 10
      ? null
      : "候选数必须是 1–10 的整数。";
  const retroCandidates = retroData?.candidates ?? [];
  const selectedRetroCandidateIndexClamped = retroCandidates.length
    ? Math.min(selectedRetroCandidateIndex, retroCandidates.length - 1)
    : 0;
  const selectedRetroCandidate =
    retroCandidates[selectedRetroCandidateIndexClamped] ?? null;
  const validCandidateCount =
    retroCandidates.filter((candidate) => candidate.valid_smiles).length;
  const retroResultStatus = isRetrosynthesizing
    ? "运行中"
    : retroError
      ? "运行失败"
      : retroData
        ? retroCandidates.length
          ? `${retroData.total} 个候选`
          : "未找到候选"
        : "等待运行";
  const assistantTaskStatus = isRetrosynthesizing
    ? "反推运行中"
    : retroError
      ? "反推需检查"
      : retroData
        ? `反推 ${retroData.total} 个候选`
        : "反推待运行";
  const operationBusy = canvas.isBusy || isRetrosynthesizing || Boolean(openingModuleId);

  function restorePanelFocus(panel: Exclude<OpenWorkbenchPanel, null>) {
    const target = panel === "modules" ? moduleButtonRef.current : assistantButtonRef.current;
    window.requestAnimationFrame(() => target?.focus());
  }

  function closePanel(restoreFocus = true) {
    if (openPanel && restoreFocus) {
      restorePanelFocus(openPanel);
    }
    setOpenPanel(null);
  }

  function togglePanel(panel: Exclude<OpenWorkbenchPanel, null>) {
    if (openPanel === panel) {
      closePanel(true);
      return;
    }
    setOpenPanel(panel);
  }

  function showModuleGrid() {
    setModulePanelView("grid");
    setSelectedModuleName("尚未选择任务");
  }

  function showRetrosynthesisParameters() {
    setModulePanelView("retrosynthesis");
    setSelectedModuleName("单体反推");
    setRetroError(null);
  }

  async function openExternalModule(module: WorkbenchModule) {
    setSelectedModuleName(module.shortName);
    setOpeningModuleId(module.id);
    try {
      await canvas.syncSmilesFromCanvas({ preserveExisting: true, quiet: true });
    } finally {
      setOpeningModuleId(null);
      closePanel(false);
      onOpenModule(module.id);
    }
  }

  async function useCurrentStructureForRetrosynthesis() {
    const currentSmiles = await canvas.syncSmilesFromCanvas({
      preserveExisting: true,
      quiet: true
    });
    if (!currentSmiles) {
      canvas.setFeedback("当前结构为空，请先绘制、导入或加载结构。");
      return;
    }
    setRetroSmiles(currentSmiles);
    setShowRetroValidation(false);
    setRetroError(null);
  }

  async function submitRetrosynthesis(event?: FormEvent<HTMLFormElement>) {
    event?.preventDefault();
    setShowRetroValidation(true);
    if (retroTargetValidation || retroCountValidation) {
      return;
    }

    setHasRetroRun(true);
    setIsDrawerOpen(true);
    closePanel(false);
    setIsRetrosynthesizing(true);
    setRetroError(null);
    setRetroData(null);
    setSelectedRetroCandidateIndex(0);

    try {
      const data = await predictMonomerPrecursors({
        smiles: retroSmiles.trim(),
        target_role: retroTargetRole,
        num_beams: Math.max(5, parsedRetroReturnCount),
        num_return_sequences: parsedRetroReturnCount,
        max_new_tokens: 128
      });
      setRetroData(data);
    } catch (error) {
      console.error("Failed to run monomer retrosynthesis", error);
      setRetroError(error instanceof Error ? error.message : "单体逆合成反推失败。");
    } finally {
      setIsRetrosynthesizing(false);
    }
  }

  function openRetroParametersFromDrawer() {
    setIsDrawerOpen(false);
    setModulePanelView("retrosynthesis");
    setSelectedModuleName("单体反推");
    setOpenPanel("modules");
  }

  function moveRetroCandidate(delta: number) {
    setSelectedRetroCandidateIndex((currentIndex) => {
      if (!retroCandidates.length) {
        return 0;
      }
      return (currentIndex + delta + retroCandidates.length) % retroCandidates.length;
    });
  }

  function copyText(value: string | null | undefined) {
    if (value) {
      void navigator.clipboard?.writeText(value);
    }
  }

  function handleAssistantSend() {
    if (!assistantInput.trim()) {
      return;
    }
    setAssistantNotice("AI 对话接口尚未接入，本次内容未发送。");
  }

  useEffect(() => {
    if (!openPanel) {
      return;
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      const panel =
        openPanel === "modules" ? modulePanelRef.current : assistantPanelRef.current;
      const trigger =
        openPanel === "modules" ? moduleButtonRef.current : assistantButtonRef.current;
      if (!panel?.contains(target) && !trigger?.contains(target)) {
        closePanel(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closePanel(true);
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [openPanel]);

  useEffect(() => {
    if (!openPanel) {
      return;
    }
    const panel =
      openPanel === "modules" ? modulePanelRef.current : assistantPanelRef.current;
    const frame = window.requestAnimationFrame(() => {
      panel
        ?.querySelector<HTMLElement>(
          modulePanelView === "retrosynthesis"
            ? ".sw-module-back, textarea, button"
            : "button, textarea"
        )
        ?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [modulePanelView, openPanel]);

  useEffect(() => {
    function handlePointerMove(event: PointerEvent) {
      const resizeState = resizeStateRef.current;
      if (!resizeState) {
        return;
      }
      const nextWidth = resizeState.startWidth + resizeState.startX - event.clientX;
      setDrawerWidth(clamp(nextWidth, DRAWER_MIN_WIDTH, DRAWER_MAX_WIDTH));
    }

    function stopResize() {
      resizeStateRef.current = null;
      document.body.classList.remove("tg-is-resizing");
    }

    document.addEventListener("pointermove", handlePointerMove);
    document.addEventListener("pointerup", stopResize);
    document.addEventListener("pointercancel", stopResize);
    return () => {
      document.removeEventListener("pointermove", handlePointerMove);
      document.removeEventListener("pointerup", stopResize);
      document.removeEventListener("pointercancel", stopResize);
      document.body.classList.remove("tg-is-resizing");
    };
  }, []);

  function startDrawerResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    resizeStateRef.current = {
      startX: event.clientX,
      startWidth: drawerWidth
    };
    document.body.classList.add("tg-is-resizing");
  }

  function renderRetroDrawerBody() {
    if (isRetrosynthesizing) {
      return (
        <div className="tg-result-state">
          <span className="tg-result-state-icon">
            <LoaderCircle className="animate-spin" />
          </span>
          <strong>正在生成逆合成候选</strong>
          <p>模型正在分析目标单体并整理可能的前体组合。</p>
        </div>
      );
    }

    if (retroError) {
      return (
        <div className="tg-result-state is-danger">
          <span className="tg-result-state-icon">
            <TriangleAlert />
          </span>
          <strong>反推运行失败</strong>
          <p>{retroError}</p>
          <button
            type="button"
            className="sw-result-action"
            onClick={openRetroParametersFromDrawer}
          >
            调整反推参数
          </button>
        </div>
      );
    }

    if (!retroData) {
      return (
        <div className="tg-result-state">
          <span className="tg-result-state-icon">
            <Network />
          </span>
          <strong>等待反推</strong>
          <p>从功能参数中选择“单体逆合成反推”，输入目标单体后运行。</p>
        </div>
      );
    }

    if (!retroCandidates.length) {
      return (
        <div className="tg-result-state">
          <span className="tg-result-state-icon">
            <Search />
          </span>
          <strong>未找到可展示候选</strong>
          <p>可调整目标结构、结构提示或候选数后重新运行。</p>
          <button
            type="button"
            className="sw-result-action"
            onClick={openRetroParametersFromDrawer}
          >
            调整反推参数
          </button>
        </div>
      );
    }

    return (
      <div className="sw-retro-results">
        <div className="sw-result-summary">
          <div>
            <span>识别类型</span>
            <strong>{TARGET_ROLE_LABEL[retroData.inferred_target_role]}</strong>
          </div>
          <div>
            <span>合法候选</span>
            <strong>
              {validCandidateCount}/{retroData.total}
            </strong>
          </div>
        </div>

        <div className="sw-target-card">
          <div>
            <span>目标结构</span>
            <code>{retroData.canonical_smiles}</code>
          </div>
          <button
            type="button"
            onClick={() => copyText(retroData.canonical_smiles)}
            aria-label="复制目标 SMILES"
          >
            <Copy />
          </button>
        </div>

        {selectedRetroCandidate ? (
          <article className="sw-candidate-card">
            <header>
              <div>
                <div className="sw-candidate-badges">
                  <Badge className="border border-slate-200 bg-slate-50 text-slate-700">
                    候选 {selectedRetroCandidate.rank}
                  </Badge>
                  <Badge
                    className={
                      selectedRetroCandidate.valid_smiles
                        ? "border border-emerald-200 bg-emerald-50 text-emerald-700"
                        : "border border-rose-200 bg-rose-50 text-rose-700"
                    }
                  >
                    {selectedRetroCandidate.valid_smiles ? "合法 SMILES" : "需人工校验"}
                  </Badge>
                </div>
                <h3>{selectedRetroCandidate.reaction_hint}</h3>
              </div>
              <div className="sw-candidate-pager">
                <button
                  type="button"
                  onClick={() => moveRetroCandidate(-1)}
                  disabled={retroCandidates.length <= 1}
                  aria-label="查看上一个候选"
                >
                  <ChevronLeft />
                </button>
                <span>
                  {selectedRetroCandidateIndexClamped + 1}/{retroCandidates.length}
                </span>
                <button
                  type="button"
                  onClick={() => moveRetroCandidate(1)}
                  disabled={retroCandidates.length <= 1}
                  aria-label="查看下一个候选"
                >
                  <ChevronRight />
                </button>
              </div>
            </header>

            <div className="sw-reactant-list">
              {selectedRetroCandidate.reactants.map((reactant, index) => (
                <div
                  key={`${selectedRetroCandidate.rank}-${index}-${reactant.input_smiles}`}
                  className="sw-reactant"
                >
                  <div>
                    <span>前体 {index + 1}</span>
                    <code>{reactant.canonical_smiles ?? reactant.input_smiles}</code>
                  </div>
                  <button
                    type="button"
                    onClick={() =>
                      copyText(reactant.canonical_smiles ?? reactant.input_smiles)
                    }
                    aria-label={`复制前体 ${index + 1} SMILES`}
                  >
                    <Copy />
                  </button>
                </div>
              ))}
            </div>
          </article>
        ) : null}
      </div>
    );
  }

  const rootStyle = {
    "--tg-drawer-width": `${drawerWidth}px`
  } as CSSProperties;

  return (
    <div
      className={`polymer-desktop-page polymer-desktop-page--embedded tg-reverse-page structure-workbench-page${isDrawerOpen ? " has-open-drawer" : ""}`}
      style={rootStyle}
    >
      <h1 className="tg-page-title">结构工作台</h1>

      <div className="tg-workbench-shell">
        <div className="tg-workbench-column">
          <header className="polymer-module-header tg-toolbar-row">
            <input
              ref={canvas.fileInputRef}
              className="tg-visually-hidden"
              type="file"
              accept="image/*"
              aria-label="导入结构图片"
              onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                if (file) {
                  void canvas.importImageFile(file);
                }
              }}
            />

            <div className="header-actions tg-toolbar" aria-label="结构工作台工具栏">
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button sw-toolbar-action"
                data-workbench-tool="load"
                onClick={() => void canvas.loadStructure(REVERSE_DESIGN_DEMO_SMILES)}
                disabled={operationBusy}
              >
                {canvas.isLoadingStructure ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <Atom />
                )}
                加载结构
              </button>
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button sw-toolbar-action"
                data-workbench-tool="import"
                onClick={() => canvas.fileInputRef.current?.click()}
                disabled={operationBusy}
              >
                {canvas.isImportingImage ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <ImagePlus />
                )}
                导入图片
              </button>
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button sw-toolbar-action"
                data-workbench-tool="clear"
                onClick={() => void canvas.clearCanvas()}
                disabled={operationBusy || !canvas.isEditorReady}
              >
                {canvas.isClearing ? <LoaderCircle className="animate-spin" /> : <Eraser />}
                清空画布
              </button>
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button sw-toolbar-action"
                data-workbench-tool="sync"
                onClick={() => void canvas.syncSmilesFromCanvas()}
                disabled={operationBusy || !canvas.isEditorReady}
              >
                {canvas.isSyncing ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <RefreshCcw />
                )}
                生成SMILES
              </button>
              <button
                type="button"
                className={`btn btn--outline btn--sm tg-tool-button sw-toolbar-action${canvas.isFlipped ? " active" : ""}`}
                data-workbench-tool="3d"
                id="btn-toggle-3d"
                onClick={() => void canvas.toggle3D()}
                disabled={operationBusy || !canvas.isEditorReady}
              >
                {canvas.isFlipping ? <LoaderCircle className="animate-spin" /> : <Box />}
                {canvas.isFlipped ? "2D画布" : "3D构象"}
              </button>
              <span className="tg-toolbar-separator" aria-hidden="true" />
              <button
                ref={moduleButtonRef}
                type="button"
                className={`btn btn--outline btn--sm tg-icon-tool sw-toolbar-action${openPanel === "modules" ? " is-active" : ""}`}
                data-workbench-tool="modules"
                aria-label="功能参数"
                title="功能参数"
                aria-expanded={openPanel === "modules"}
                aria-controls="structure-module-panel"
                onClick={() => togglePanel("modules")}
              >
                <SlidersHorizontal />
              </button>
              <button
                ref={assistantButtonRef}
                type="button"
                className={`btn btn--outline btn--sm tg-icon-tool sw-toolbar-action${openPanel === "assistant" ? " is-active" : ""}`}
                data-workbench-tool="assistant"
                aria-label="AI 助手"
                title="AI 助手"
                aria-expanded={openPanel === "assistant"}
                aria-controls="structure-assistant-panel"
                onClick={() => togglePanel("assistant")}
              >
                <Sparkles />
              </button>
            </div>

            <section
              ref={modulePanelRef}
              id="structure-module-panel"
              className={`tg-parameter-panel sw-module-panel${openPanel === "modules" ? " is-open" : ""}`}
              role="dialog"
              aria-modal="false"
              aria-labelledby="structure-module-panel-title"
              aria-hidden={openPanel !== "modules"}
              inert={openPanel !== "modules"}
            >
              <header className="sw-module-panel-header">
                <div className="sw-panel-heading">
                  {modulePanelView === "retrosynthesis" ? (
                    <button
                      type="button"
                      className="sw-module-back"
                      aria-label="返回功能列表"
                      onClick={showModuleGrid}
                    >
                      <ArrowLeft />
                    </button>
                  ) : null}
                  <span className="sw-panel-mark">
                    {modulePanelView === "retrosynthesis" ? <Route /> : <Grid2X2 />}
                  </span>
                  <span>
                    <h2 id="structure-module-panel-title">
                      {modulePanelView === "retrosynthesis"
                        ? "单体逆合成反推"
                        : "选择功能"}
                    </h2>
                    <small>
                      {modulePanelView === "retrosynthesis"
                        ? "工作台内置任务"
                        : "共享当前结构，进入下一项科研任务"}
                    </small>
                  </span>
                </div>
                <button
                  type="button"
                  aria-label="收起功能参数"
                  onClick={() => closePanel()}
                >
                  <X />
                </button>
              </header>

              {modulePanelView === "grid" ? (
                <div className="sw-module-grid-view">
                  <div className="sw-module-grid-intro">
                    <span>8 项功能</span>
                  </div>
                  <div className="sw-module-grid" aria-label="使用共享结构的功能模块">
                    {WORKBENCH_MODULES.map((module) => {
                      const Icon = module.icon;
                      const isOpening = openingModuleId === module.id;
                      return (
                        <button
                          key={module.id}
                          type="button"
                          className={`sw-module-tile is-${module.relationship}`}
                          aria-label={
                            module.isBuiltIn
                              ? `设置${module.name}参数`
                              : `打开${module.name}`
                          }
                          disabled={Boolean(openingModuleId)}
                          onClick={() => {
                            if (module.isBuiltIn) {
                              showRetrosynthesisParameters();
                            } else {
                              void openExternalModule(module);
                            }
                          }}
                        >
                          {module.isBuiltIn ? (
                            <span className="sw-local-dot" aria-hidden="true" />
                          ) : null}
                          <span className="sw-module-tile-icon">
                            {isOpening ? <LoaderCircle className="animate-spin" /> : <Icon />}
                          </span>
                          <strong>{module.shortName}</strong>
                        </button>
                      );
                    })}
                  </div>
                  <div className="sw-module-legend" aria-label="模块与画板关系图例">
                    {(
                      [
                        ["direct", "直接使用画板"],
                        ["shared", "消费共享结构"],
                        ["optional", "结构输入可选"],
                        ["local", "工作台内置"]
                      ] as const
                    ).map(([relationship, label]) => (
                      <span key={relationship} className={`sw-relationship is-${relationship}`}>
                        {label}
                      </span>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="sw-retro-detail">
                  <div className="sw-retro-hero">
                    <span>
                      <Route />
                    </span>
                    <div>
                      <div>
                        <h3>单体逆合成反推</h3>
                        <span className="sw-relationship is-local">工作台内置</span>
                      </div>
                      <p>
                        输入目标单体并直接运行反推。候选和反应提示会进入右侧结果抽屉。
                      </p>
                    </div>
                  </div>

                  <form
                    className="sw-retro-form"
                    noValidate
                    onSubmit={(event) => void submitRetrosynthesis(event)}
                  >
                    <label className="sw-field">
                      <span className="sw-field-label">
                        <span>Target monomer SMILES</span>
                        <small>必填</small>
                      </span>
                      <span
                        className={`sw-retro-textarea${showRetroValidation && retroTargetValidation ? " is-invalid" : ""}`}
                      >
                        <textarea
                          rows={3}
                          value={retroSmiles}
                          onChange={(event) => {
                            setRetroSmiles(event.currentTarget.value);
                            setRetroError(null);
                          }}
                          placeholder={`例如：${DEFAULT_RETROSYNTHESIS_MONOMER_SMILES}`}
                          spellCheck={false}
                          aria-label="目标单体 SMILES"
                          aria-describedby="sw-retro-target-error"
                        />
                      </span>
                      <span id="sw-retro-target-error" className="sw-field-error" role="status">
                        {showRetroValidation ? retroTargetValidation : null}
                      </span>
                    </label>

                    <div className="sw-field-row">
                      <label className="sw-field">
                        <span className="sw-field-label">
                          <span>结构提示</span>
                        </span>
                        <span className="sw-retro-control sw-retro-select">
                          <select
                            value={retroTargetRole}
                            onChange={(event) =>
                              setRetroTargetRole(
                                event.currentTarget.value as MonomerRetrosynthesisTargetRole
                              )
                            }
                            aria-label="反推结构提示"
                          >
                            {TARGET_ROLE_OPTIONS.map((option) => (
                              <option key={option.value} value={option.value}>
                                {option.label}
                              </option>
                            ))}
                          </select>
                          <ChevronDown aria-hidden="true" />
                        </span>
                      </label>

                      <label className="sw-field">
                        <span className="sw-field-label">
                          <span>候选数</span>
                          <small>1–10</small>
                        </span>
                        <span
                          className={`sw-retro-control${showRetroValidation && retroCountValidation ? " is-invalid" : ""}`}
                        >
                          <input
                            type="number"
                            min={1}
                            max={10}
                            step={1}
                            inputMode="numeric"
                            value={retroReturnCount}
                            onChange={(event) =>
                              setRetroReturnCount(event.currentTarget.value)
                            }
                            aria-label="反推候选数"
                            aria-describedby="sw-retro-count-error"
                          />
                        </span>
                      </label>
                    </div>
                    <span id="sw-retro-count-error" className="sw-field-error" role="status">
                      {showRetroValidation ? retroCountValidation : null}
                    </span>

                    <div className="sw-retro-actions">
                      <button
                        type="button"
                        className="sw-secondary-button"
                        onClick={() => void useCurrentStructureForRetrosynthesis()}
                        disabled={operationBusy}
                      >
                        <Atom />
                        使用当前结构
                      </button>
                      <button
                        type="submit"
                        className="sw-primary-button"
                        disabled={operationBusy}
                      >
                        {isRetrosynthesizing ? (
                          <LoaderCircle className="animate-spin" />
                        ) : (
                          <Sparkles />
                        )}
                        运行反推
                      </button>
                    </div>
                  </form>
                </div>
              )}
            </section>

            <section
              ref={assistantPanelRef}
              id="structure-assistant-panel"
              className={`tg-assistant-panel${openPanel === "assistant" ? " is-open" : ""}`}
              role="dialog"
              aria-modal="false"
              aria-labelledby="structure-assistant-title"
              aria-hidden={openPanel !== "assistant"}
              inert={openPanel !== "assistant"}
            >
              <header className="tg-assistant-header">
                <div>
                  <span className="tg-assistant-mark">
                    <Sparkles />
                  </span>
                  <span>
                    <h2 id="structure-assistant-title">结构 AI 助手</h2>
                    <small>当前科研上下文已连接</small>
                  </span>
                </div>
                <span className="tg-assistant-header-actions">
                  <button
                    type="button"
                    aria-label="新建对话"
                    title="新建对话"
                    onClick={() => {
                      setAssistantInput("");
                      setAssistantNotice(null);
                    }}
                  >
                    <Plus />
                  </button>
                  <button
                    type="button"
                    aria-label="收起 AI 助手"
                    onClick={() => closePanel()}
                  >
                    <X />
                  </button>
                </span>
              </header>

              <div className="tg-assistant-body">
                <div className="tg-assistant-context" aria-label="当前 AI 上下文">
                  <span className={structure.smiles.trim() ? "is-ready" : ""}>
                    <i />
                    {structure.smiles.trim() ? "共享结构已同步" : "暂无共享结构"}
                  </span>
                  <span>{selectedModuleName}</span>
                  <span>{assistantTaskStatus}</span>
                </div>

                <div className="tg-assistant-welcome">
                  <span className="tg-assistant-orb">
                    <Sparkles />
                  </span>
                  <h3>
                    <em>你好，</em>
                    <br />
                    今天想一起研究什么？
                  </h3>
                  <p>我会结合当前共享结构、所选功能与单体反推状态辅助分析。</p>
                  <div className="tg-assistant-suggestions">
                    {[
                      "解释当前结构中的主要官能团",
                      "推荐适合当前结构的下一步模块",
                      "梳理单体反推结果中的前体差异"
                    ].map((suggestion) => (
                      <button
                        key={suggestion}
                        type="button"
                        onClick={() => {
                          setAssistantInput(suggestion);
                          setAssistantNotice(null);
                        }}
                      >
                        <Sparkles />
                        {suggestion}
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <footer className="tg-assistant-composer">
                <div className="tg-assistant-input-shell">
                  <textarea
                    rows={2}
                    value={assistantInput}
                    onChange={(event) => {
                      setAssistantInput(event.currentTarget.value);
                      setAssistantNotice(null);
                    }}
                    placeholder="向 AI 助手提问，或描述新的结构约束…"
                    aria-label="发送给 AI 助手的消息"
                  />
                  <div>
                    <span>
                      <Plus /> 科研助手
                    </span>
                    <button
                      type="button"
                      aria-label="发送消息"
                      onClick={handleAssistantSend}
                      disabled={!assistantInput.trim()}
                    >
                      <ArrowUp />
                    </button>
                  </div>
                </div>
                <small role="status">
                  {assistantNotice || "界面设计预留 · 当前不会向 AI 模型发送数据"}
                </small>
              </footer>
            </section>
          </header>

          <section className="tg-structure-surface" aria-label="共享结构画板">
            <div className={`tg-structure-flip${canvas.isFlipped ? " is-flipped" : ""}`}>
              <div className="tg-structure-face tg-structure-face-front">
                <iframe
                  ref={structure.iframeRef}
                  title="结构工作台结构编辑器"
                  src="/ketcher/index.html"
                  onLoad={canvas.handleEditorLoad}
                />
              </div>
              <div
                className="tg-structure-face tg-structure-face-back"
                aria-hidden={!canvas.isFlipped}
              >
                <StructurePreview3D
                  smiles={structure.smiles}
                  variant="bare"
                  visualStyle="polished-atoms"
                  className="h-full"
                  previewClassName="h-full min-h-0"
                />
              </div>
            </div>
          </section>

          <section className="tg-smiles-capsule" aria-labelledby="structure-smiles-label">
            <label id="structure-smiles-label">SMILES</label>
            <textarea
              rows={2}
              readOnly
              value={structure.smiles}
              placeholder="在上方 Ketcher 画布绘制结构后，点击“生成SMILES”。"
              aria-label="当前共享 SMILES，只读"
            />
            <button
              type="button"
              onClick={() => void canvas.copySmiles()}
              disabled={!structure.smiles.trim()}
              aria-label="复制共享 SMILES"
              title="复制共享 SMILES"
            >
              {canvas.copyState === "copied" ? <Check /> : <Copy />}
            </button>
            {canvas.feedback ? (
              <p role="status" aria-live="polite">
                {canvas.feedback}
              </p>
            ) : null}
          </section>
        </div>
      </div>

      <aside
        className={`tg-results-drawer${isDrawerOpen ? " is-open" : ""}`}
        aria-hidden={!isDrawerOpen}
        inert={!isDrawerOpen}
        aria-labelledby="structure-retro-results-title"
      >
        <div
          className="tg-drawer-resizer"
          role="separator"
          tabIndex={isDrawerOpen ? 0 : -1}
          aria-label="调整单体反推结果抽屉宽度"
          aria-orientation="vertical"
          aria-valuemin={DRAWER_MIN_WIDTH}
          aria-valuemax={DRAWER_MAX_WIDTH}
          aria-valuenow={drawerWidth}
          onPointerDown={startDrawerResize}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
              event.preventDefault();
              const amount = event.shiftKey ? 40 : 16;
              setDrawerWidth((current) =>
                clamp(
                  current + (event.key === "ArrowLeft" ? amount : -amount),
                  DRAWER_MIN_WIDTH,
                  DRAWER_MAX_WIDTH
                )
              );
            }
          }}
        />
        <header className="tg-results-header">
          <div>
            <span>
              <MessageSquareText />
            </span>
            <div>
              <h2 id="structure-retro-results-title">单体反推结果</h2>
              <p>{retroResultStatus}</p>
            </div>
          </div>
          <button
            type="button"
            aria-label="关闭单体反推结果"
            onClick={() => setIsDrawerOpen(false)}
          >
            <X />
          </button>
        </header>
        <div className="tg-results-body" aria-live="polite">
          {renderRetroDrawerBody()}
        </div>
      </aside>

      {hasRetroRun && !isDrawerOpen ? (
        <button
          type="button"
          className="btn-expand-analysis tg-drawer-reopen"
          onClick={() => setIsDrawerOpen(true)}
          aria-label="展开单体反推结果"
          title="展开单体反推结果"
        >
          <Route width={14} height={14} />
        </button>
      ) : null}
    </div>
  );
}
