import { Atom, CircleCheck, LoaderCircle, PenLine } from "lucide-react";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type FormEvent
} from "react";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import { fetchStructure2D, predictMonomerPrecursors } from "../services/api";
import type {
  MonomerRetrosynthesisResponse,
  MonomerRetrosynthesisTargetRole,
  StructureWorkspaceContext
} from "../types";
import "../styles/structure-workbench.css";
import {
  StructureCanvasSurface,
  type StructureUtilityPanel
} from "./structure-workbench/StructureCanvasSurface";
import { RetrosynthesisDrawer } from "./structure-workbench/RetrosynthesisDrawer";
import {
  StructureUtilityPanels,
  type StructureModulePanelView,
  type StructureWorkbenchModuleId
} from "./structure-workbench/StructureUtilityPanels";

export type { StructureWorkbenchModuleId } from "./structure-workbench/StructureUtilityPanels";
export { CurrentStructurePanel, MissingStructurePanel, WorkbenchPanel } from "./CurrentStructurePanel";

export type StructureWorkbenchHandle = {
  syncBeforeLeave(): Promise<void>;
};

type StructureWorkbenchPageProps = {
  structure: StructureWorkspaceContext;
  onOpenModule: (moduleId: StructureWorkbenchModuleId) => void;
};

const DEFAULT_RETROSYNTHESIS_MONOMER_SMILES = "C=C(C)C(=O)OC";
const MOBILE_TEXT_MODE_QUERY = "(max-width: 767px)";

function viewportUsesTextMode() {
  return typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(MOBILE_TEXT_MODE_QUERY).matches;
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

export const StructureWorkbenchPage = forwardRef<
  StructureWorkbenchHandle,
  StructureWorkbenchPageProps
>(function StructureWorkbenchPage({ structure, onOpenModule }, forwardedRef) {
  const [openPanel, setOpenPanel] = useState<StructureUtilityPanel>(null);
  const [modulePanelView, setModulePanelView] = useState<StructureModulePanelView>("grid");
  const [selectedModuleName, setSelectedModuleName] = useState("尚未选择任务");
  const [openingModuleId, setOpeningModuleId] = useState<StructureWorkbenchModuleId | null>(null);
  const [assistantInput, setAssistantInput] = useState("");
  const [assistantNotice, setAssistantNotice] = useState<string | null>(null);

  const initialSharedSmiles = structure.smiles.trim();
  const draftBaseRef = useRef(initialSharedSmiles);
  const [smilesDraft, setSmilesDraft] = useState(initialSharedSmiles);
  const [isDraftDirty, setIsDraftDirty] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [hasSharedConflict, setHasSharedConflict] = useState(false);
  const [isApplyingDraft, setIsApplyingDraft] = useState(false);

  const [isMobile, setIsMobile] = useState(viewportUsesTextMode);
  const [hasMountedEditor, setHasMountedEditor] = useState(() => !viewportUsesTextMode());
  const [isCanvasExpanded, setIsCanvasExpanded] = useState(() => !viewportUsesTextMode());
  const [hasActivated3D, setHasActivated3D] = useState(false);
  const [previewSvg, setPreviewSvg] = useState<string | null>(null);
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [retroSmiles, setRetroSmiles] = useState(DEFAULT_RETROSYNTHESIS_MONOMER_SMILES);
  const [retroTargetRole, setRetroTargetRole] = useState<MonomerRetrosynthesisTargetRole>("auto");
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
  const restoreFocusFrameRef = useRef<number | null>(null);
  const retroAbortRef = useRef<AbortController | null>(null);
  const retroRequestRevisionRef = useRef(0);

  const handleStructureChanged = useCallback(() => setAssistantNotice(null), []);
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
  const operationBusy = canvas.isBusy || isApplyingDraft || Boolean(openingModuleId);

  const acceptSharedStructure = useCallback((value: string) => {
    const normalized = value.trim();
    draftBaseRef.current = normalized;
    setSmilesDraft(normalized);
    setIsDraftDirty(false);
    setHasSharedConflict(false);
    setDraftError(null);
  }, []);

  useEffect(() => {
    const nextSharedSmiles = structure.smiles.trim();
    if (!isDraftDirty) {
      draftBaseRef.current = nextSharedSmiles;
      setSmilesDraft(nextSharedSmiles);
      setHasSharedConflict(false);
      return;
    }
    if (nextSharedSmiles !== draftBaseRef.current) {
      setHasSharedConflict(true);
    }
  }, [isDraftDirty, structure.smiles]);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(MOBILE_TEXT_MODE_QUERY);
    const handleChange = (event: MediaQueryListEvent) => setIsMobile(event.matches);
    setIsMobile(media.matches);
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, []);

  useEffect(() => {
    if (!isMobile) {
      setHasMountedEditor(true);
      setIsCanvasExpanded(true);
    }
  }, [isMobile]);

  useEffect(() => {
    const shouldLoadPreview = isMobile && (!hasMountedEditor || !isCanvasExpanded);
    const source = structure.smiles.trim();
    if (!shouldLoadPreview || !source) {
      if (!source) {
        setPreviewSvg(null);
        setPreviewError(null);
        setIsPreviewLoading(false);
      }
      return;
    }

    const controller = new AbortController();
    setIsPreviewLoading(true);
    setPreviewError(null);
    void fetchStructure2D(source, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setPreviewSvg(result.structure_svg);
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setPreviewSvg(null);
          setPreviewError(errorMessage(error, "二维摘要生成失败"));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsPreviewLoading(false);
      });
    return () => controller.abort();
  }, [hasMountedEditor, isCanvasExpanded, isMobile, structure.smiles]);

  useEffect(() => {
    return () => {
      if (restoreFocusFrameRef.current !== null) {
        window.cancelAnimationFrame(restoreFocusFrameRef.current);
        restoreFocusFrameRef.current = null;
      }
      retroRequestRevisionRef.current += 1;
      retroAbortRef.current?.abort();
      retroAbortRef.current = null;
    };
  }, []);

  useImperativeHandle(
    forwardedRef,
    () => ({
      async syncBeforeLeave() {
        if (hasMountedEditor) {
          await canvas.syncSmilesFromCanvas({ preserveExisting: true, quiet: true });
        }
      }
    }),
    [canvas, hasMountedEditor]
  );

  const restorePanelFocus = useCallback((panel: Exclude<StructureUtilityPanel, null>) => {
    const target = panel === "modules" ? moduleButtonRef.current : assistantButtonRef.current;
    if (restoreFocusFrameRef.current !== null) {
      window.cancelAnimationFrame(restoreFocusFrameRef.current);
    }
    restoreFocusFrameRef.current = window.requestAnimationFrame(() => {
      restoreFocusFrameRef.current = null;
      target?.focus();
    });
  }, []);

  const closePanel = useCallback(
    (restoreFocus = true) => {
      if (openPanel && restoreFocus) restorePanelFocus(openPanel);
      setOpenPanel(null);
    },
    [openPanel, restorePanelFocus]
  );

  function togglePanel(panel: Exclude<StructureUtilityPanel, null>) {
    if (openPanel === panel) {
      closePanel(true);
    } else {
      setOpenPanel(panel);
    }
  }

  useEffect(() => {
    if (!openPanel) return;

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      const panel = openPanel === "modules" ? modulePanelRef.current : assistantPanelRef.current;
      const trigger = openPanel === "modules" ? moduleButtonRef.current : assistantButtonRef.current;
      if (!panel?.contains(target) && !trigger?.contains(target)) closePanel(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closePanel(true);
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [closePanel, openPanel]);

  useEffect(() => {
    if (!openPanel) return;
    const panel = openPanel === "modules" ? modulePanelRef.current : assistantPanelRef.current;
    const frame = window.requestAnimationFrame(() => {
      panel
        ?.querySelector<HTMLElement>(
          modulePanelView === "retrosynthesis"
            ? ".np-sw-module-back, textarea, button"
            : "button, textarea"
        )
        ?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [modulePanelView, openPanel]);

  async function applyDraftValue(source: string) {
    const normalized = source.trim();
    if (!normalized) {
      setDraftError("请输入要应用的 SMILES。");
      return false;
    }
    setIsApplyingDraft(true);
    setDraftError(null);
    try {
      const result = await canvas.applyTextStructure(normalized);
      if (!result.applied) {
        setDraftError(canvas.feedback || "结构未能应用，请检查画布状态后重试。");
        return false;
      }
      acceptSharedStructure(result.smiles);
      return true;
    } catch (error) {
      setDraftError(errorMessage(error, "SMILES 标准化失败，请检查结构后重试。"));
      return false;
    } finally {
      setIsApplyingDraft(false);
    }
  }

  async function loadExample() {
    setSmilesDraft(REVERSE_DESIGN_DEMO_SMILES);
    setIsDraftDirty(true);
    if (!hasMountedEditor) {
      await applyDraftValue(REVERSE_DESIGN_DEMO_SMILES);
      return;
    }
    const loaded = await canvas.loadStructure(REVERSE_DESIGN_DEMO_SMILES);
    if (loaded) {
      const synchronized = await canvas.syncSmilesFromCanvas({ preserveExisting: true, quiet: true });
      acceptSharedStructure(synchronized);
    }
  }

  async function importImage(file: File) {
    const imported = await canvas.importImageFile(file);
    if (imported) {
      const synchronized = await canvas.syncSmilesFromCanvas({ preserveExisting: true, quiet: true });
      acceptSharedStructure(synchronized);
    }
  }

  async function clearStructure() {
    const cleared = await canvas.clearCanvas();
    if (cleared) acceptSharedStructure("");
  }

  async function syncFromCanvas() {
    const synchronized = await canvas.syncSmilesFromCanvas();
    acceptSharedStructure(synchronized);
  }

  async function toggle3D() {
    const activating = !canvas.isFlipped;
    const changed = await canvas.toggle3D();
    if (changed && activating) setHasActivated3D(true);
  }

  async function openExternalModule(id: StructureWorkbenchModuleId, shortName: string) {
    if (openingModuleId) return;
    setSelectedModuleName(shortName);
    setOpeningModuleId(id);
    try {
      if (hasMountedEditor) {
        await canvas.syncSmilesFromCanvas({ preserveExisting: true, quiet: true });
      }
    } finally {
      setOpeningModuleId(null);
      closePanel(false);
      onOpenModule(id);
    }
  }

  async function useCurrentStructureForRetrosynthesis() {
    const currentSmiles = hasMountedEditor
      ? await canvas.syncSmilesFromCanvas({ preserveExisting: true, quiet: true })
      : structure.smiles.trim();
    if (!currentSmiles) {
      canvas.setFeedback("当前结构为空，请先绘制、导入或应用结构。");
      return;
    }
    setRetroSmiles(currentSmiles);
    setShowRetroValidation(false);
    setRetroError(null);
  }

  function submitRetrosynthesis(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setShowRetroValidation(true);
    if (retroTargetValidation || retroCountValidation) return;

    retroAbortRef.current?.abort();
    const controller = new AbortController();
    const requestRevision = retroRequestRevisionRef.current + 1;
    retroRequestRevisionRef.current = requestRevision;
    retroAbortRef.current = controller;

    setHasRetroRun(true);
    setIsDrawerOpen(true);
    closePanel(false);
    setIsRetrosynthesizing(true);
    setRetroError(null);
    setRetroData(null);
    setSelectedRetroCandidateIndex(0);

    void predictMonomerPrecursors(
      {
        smiles: retroSmiles.trim(),
        target_role: retroTargetRole,
        num_beams: Math.max(5, parsedRetroReturnCount),
        num_return_sequences: parsedRetroReturnCount,
        max_new_tokens: 128
      },
      controller.signal
    )
      .then((data) => {
        if (!controller.signal.aborted && retroRequestRevisionRef.current === requestRevision) {
          setRetroData(data);
        }
      })
      .catch((error) => {
        if (
          controller.signal.aborted ||
          isAbortError(error) ||
          retroRequestRevisionRef.current !== requestRevision
        ) {
          return;
        }
        console.error("Failed to run monomer retrosynthesis", error);
        setRetroError(errorMessage(error, "单体逆合成反推失败。"));
      })
      .finally(() => {
        if (retroRequestRevisionRef.current === requestRevision) {
          if (retroAbortRef.current === controller) retroAbortRef.current = null;
          setIsRetrosynthesizing(false);
        }
      });
  }

  function openRetroParametersFromDrawer() {
    setIsDrawerOpen(false);
    setModulePanelView("retrosynthesis");
    setSelectedModuleName("单体反推");
    setOpenPanel("modules");
  }

  function updateAssistantInput(value: string) {
    setAssistantInput(value);
    setAssistantNotice(null);
  }

  const editorStatus = !hasMountedEditor
    ? "文本模式"
    : canvas.isEditorReady
      ? "编辑器就绪"
      : "编辑器加载中";

  return (
    <div className="np-structure-workbench" data-module="structure-workbench">
      <div className="np-sw-page">
        <header className="np-sw-page-header">
          <div>
            <span className="np-sw-page-header__mark" aria-hidden="true"><Atom /></span>
            <span>
              <h1>结构工作台</h1>
              <p>统一管理结构输入、二维编辑、三维预览与下游科研任务</p>
            </span>
          </div>
          <div className="np-sw-page-status" aria-label="工作台状态">
            <span className={canvas.isEditorReady ? "is-ready" : ""}>
              {canvas.isEditorReady ? <CircleCheck /> : hasMountedEditor ? <LoaderCircle className="np-sw-spin" /> : <PenLine />}
              {editorStatus}
            </span>
            <span className={structure.smiles.trim() ? "is-ready" : ""}>
              <i aria-hidden="true" />
              {structure.smiles.trim() ? "共享结构已应用" : "无共享结构"}
            </span>
          </div>
        </header>

        <div className={`np-sw-layout${isDrawerOpen ? " has-open-drawer" : ""}`}>
          <main className="np-sw-workspace">
            <StructureCanvasSurface
              structure={structure}
              canvas={canvas}
              draft={smilesDraft}
              draftDirty={isDraftDirty}
              draftError={draftError}
              hasSharedConflict={hasSharedConflict}
              isMobile={isMobile}
              hasMountedEditor={hasMountedEditor}
              isCanvasExpanded={isCanvasExpanded}
              hasActivated3D={hasActivated3D}
              previewSvg={previewSvg}
              isPreviewLoading={isPreviewLoading}
              previewError={previewError}
              openPanel={openPanel}
              moduleButtonRef={moduleButtonRef}
              assistantButtonRef={assistantButtonRef}
              operationBusy={operationBusy}
              onDraftChange={(value) => {
                setSmilesDraft(value);
                setIsDraftDirty(value.trim() !== draftBaseRef.current);
                setDraftError(null);
              }}
              onApplyDraft={() => void applyDraftValue(smilesDraft)}
              onUseLatestShared={() => acceptSharedStructure(structure.smiles)}
              onLoadExample={() => void loadExample()}
              onImportFile={(file) => void importImage(file)}
              onClear={() => void clearStructure()}
              onSync={() => void syncFromCanvas()}
              onToggle3D={() => void toggle3D()}
              onTogglePanel={togglePanel}
              onOpenCanvas={() => {
                setHasMountedEditor(true);
                setIsCanvasExpanded(true);
              }}
              onCollapseCanvas={() => setIsCanvasExpanded(false)}
            />

            <StructureUtilityPanels
              openPanel={openPanel}
              modulePanelView={modulePanelView}
              modulePanelRef={modulePanelRef}
              assistantPanelRef={assistantPanelRef}
              openingModuleId={openingModuleId}
              selectedModuleName={selectedModuleName}
              structureSmiles={structure.smiles}
              assistantInput={assistantInput}
              assistantNotice={assistantNotice}
              retroSmiles={retroSmiles}
              retroTargetRole={retroTargetRole}
              retroReturnCount={retroReturnCount}
              showRetroValidation={showRetroValidation}
              retroTargetValidation={retroTargetValidation}
              retroCountValidation={retroCountValidation}
              isRetrosynthesizing={isRetrosynthesizing}
              retroError={retroError}
              retroData={retroData}
              operationBusy={operationBusy}
              onClose={closePanel}
              onShowGrid={() => {
                setModulePanelView("grid");
                setSelectedModuleName("尚未选择任务");
              }}
              onShowRetrosynthesis={() => {
                setModulePanelView("retrosynthesis");
                setSelectedModuleName("单体反推");
                setRetroError(null);
              }}
              onOpenExternal={(id, name) => void openExternalModule(id, name)}
              onUseCurrentStructure={() => void useCurrentStructureForRetrosynthesis()}
              onSubmitRetrosynthesis={submitRetrosynthesis}
              onRetroSmilesChange={(value) => {
                setRetroSmiles(value);
                setRetroError(null);
              }}
              onRetroTargetRoleChange={setRetroTargetRole}
              onRetroReturnCountChange={setRetroReturnCount}
              onAssistantInputChange={updateAssistantInput}
              onAssistantNew={() => {
                setAssistantInput("");
                setAssistantNotice(null);
              }}
              onAssistantSend={() => {
                if (assistantInput.trim()) {
                  setAssistantNotice("AI 对话接口尚未接入，本次内容未发送。");
                }
              }}
            />
          </main>

          <RetrosynthesisDrawer
            open={isDrawerOpen}
            hasRun={hasRetroRun}
            width={drawerWidth}
            loading={isRetrosynthesizing}
            error={retroError}
            data={retroData}
            selectedCandidateIndex={selectedRetroCandidateIndex}
            onWidthChange={setDrawerWidth}
            onSelectedCandidateIndexChange={setSelectedRetroCandidateIndex}
            onClose={() => setIsDrawerOpen(false)}
            onOpen={() => setIsDrawerOpen(true)}
            onAdjustParameters={openRetroParametersFromDrawer}
          />
        </div>
      </div>
    </div>
  );
});
