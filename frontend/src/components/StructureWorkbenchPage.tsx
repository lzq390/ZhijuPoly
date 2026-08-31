import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent
} from "react";
import { SlidersHorizontal, Sparkles } from "lucide-react";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import { predictMonomerPrecursors } from "../services/api";
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

export type StructureCanvasOwnerHandle = {
  syncBeforeLeave(): Promise<void>;
};

export type StructureWorkbenchHandle = StructureCanvasOwnerHandle;

type StructureWorkbenchPageProps = {
  structure: StructureWorkspaceContext;
  onOpenModule: (moduleId: StructureWorkbenchModuleId) => void;
};

const DEFAULT_RETROSYNTHESIS_MONOMER_SMILES = "C=C(C)C(=O)OC";

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

  const [hasActivated3D, setHasActivated3D] = useState(false);

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
  const operationBusy = canvas.isBusy || Boolean(openingModuleId);

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
        if (!(await canvas.flushSmilesDraft())) return;
        await canvas.syncSmilesFromCanvas({ preserveExisting: true, quiet: true });
      }
    }),
    [canvas]
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

  async function loadExample() {
    await canvas.loadStructure(REVERSE_DESIGN_DEMO_SMILES);
  }

  async function importImage(file: File) {
    await canvas.importImageFile(file);
  }

  async function clearStructure() {
    await canvas.clearCanvas();
  }

  async function syncFromCanvas() {
    await canvas.syncSmilesFromCanvas();
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
      if (!(await canvas.flushSmilesDraft())) return;
      await canvas.syncSmilesFromCanvas({ preserveExisting: true, quiet: true });
    } finally {
      setOpeningModuleId(null);
    }
    closePanel(false);
    onOpenModule(id);
  }

  async function useCurrentStructureForRetrosynthesis() {
    if (!(await canvas.flushSmilesDraft())) return;
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

  const workbenchStyle = {
    "--np-sw-drawer-width": `${drawerWidth}px`
  } as CSSProperties;

  return (
    <div
      className="np-structure-workbench"
      data-module="structure-workbench"
      style={workbenchStyle}
    >
      <div className={`np-sw-page${isDrawerOpen ? " has-open-drawer" : ""}`}>
        <h1 className="np-sw-page-title">结构工作台</h1>

        <div className={`np-sw-layout${isDrawerOpen ? " has-open-drawer" : ""}`}>
          <main className="np-sw-workspace">
            <StructureCanvasSurface
              structure={structure}
              canvas={canvas}
              hasActivated3D={hasActivated3D}
              operationBusy={operationBusy}
              utilityActions={[
                {
                  id: "modules",
                  label: "功能参数",
                  icon: <SlidersHorizontal aria-hidden="true" />,
                  active: openPanel === "modules",
                  buttonRef: moduleButtonRef,
                  controls: "structure-module-panel",
                  onClick: () => togglePanel("modules")
                },
                {
                  id: "assistant",
                  label: "AI 助手",
                  icon: <Sparkles aria-hidden="true" />,
                  active: openPanel === "assistant",
                  buttonRef: assistantButtonRef,
                  controls: "structure-assistant-panel",
                  onClick: () => togglePanel("assistant")
                }
              ]}
              onLoadExample={loadExample}
              onImportFile={importImage}
              onClear={clearStructure}
              onSync={syncFromCanvas}
              onToggle3D={toggle3D}
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
