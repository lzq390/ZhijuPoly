import { useState, type ReactNode } from "react";
import {
  ArrowLeft,
  Atom,
  Database,
  FlaskConical,
  LoaderCircle,
  Microscope,
  Orbit,
  Search,
  Sparkles,
  Upload
} from "lucide-react";
import { KetcherEditor } from "./KetcherEditor";
import { StructurePreview3D } from "./StructurePreview3D";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import { cn } from "../lib/utils";
import { standardizeSmiles } from "../services/api";
import type { StructureWorkspaceContext } from "../types";

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
  const [isTextDirty, setIsTextDirty] = useState(false);
  const [isStandardizingSmiles, setIsStandardizingSmiles] = useState(false);
  const [isLoadingTextIntoCanvas, setIsLoadingTextIntoCanvas] = useState(false);
  const [structureSyncError, setStructureSyncError] = useState<string | null>(null);
  const [structureSyncMessage, setStructureSyncMessage] = useState<string | null>(null);
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

      <div className="relative z-10 overflow-x-clip bg-[#f7f9fc]">
        <main className="relative min-w-0 overflow-x-clip bg-[#f7f9fc] px-4 py-4 md:px-6 md:py-6">
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
        </main>
      </div>
    </div>
  );
}
