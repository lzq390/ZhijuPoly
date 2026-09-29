import { requestServiceAccess } from "../auth/guestAccess";
import { BrowsingRecordingControls } from "./browsing-recording/BrowsingRecording";
import type { StructureSyncResult } from "../structure/workspace";
import { ModulePageHeader } from "./ModulePageHeader";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState
} from "react";
import { SlidersHorizontal, Sparkles } from "lucide-react";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import type { StructureWorkspaceContext } from "../types";
import "../styles/structure-workbench.css";
import {
  StructureCanvasSurface,
  type StructureUtilityPanel
} from "./structure-workbench/StructureCanvasSurface";
import {
  StructureUtilityPanels,
  type StructureWorkbenchModuleId
} from "./structure-workbench/StructureUtilityPanels";

export type { StructureWorkbenchModuleId } from "./structure-workbench/StructureUtilityPanels";
export { CurrentStructurePanel, MissingStructurePanel, WorkbenchPanel } from "./CurrentStructurePanel";

export type StructureCanvasOwnerHandle = {
  syncBeforeLeave(signal?: AbortSignal): Promise<StructureSyncResult>;
};

export type StructureWorkbenchHandle = StructureCanvasOwnerHandle;

type StructureWorkbenchPageProps = {
  structure: StructureWorkspaceContext;
  onOpenModule: (moduleId: StructureWorkbenchModuleId) => void;
};

export const StructureWorkbenchPage = forwardRef<
  StructureWorkbenchHandle,
  StructureWorkbenchPageProps
>(function StructureWorkbenchPage({ structure, onOpenModule }, forwardedRef) {
  const [openPanel, setOpenPanel] = useState<StructureUtilityPanel>(null);
  const [selectedModuleName, setSelectedModuleName] = useState("尚未选择任务");
  const [openingModuleId, setOpeningModuleId] = useState<StructureWorkbenchModuleId | null>(null);
  const [assistantInput, setAssistantInput] = useState("");
  const [assistantNotice, setAssistantNotice] = useState<string | null>(null);

  const [hasActivated3D, setHasActivated3D] = useState(false);

  const modulePanelRef = useRef<HTMLElement | null>(null);
  const assistantPanelRef = useRef<HTMLElement | null>(null);
  const moduleButtonRef = useRef<HTMLButtonElement | null>(null);
  const assistantButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusFrameRef = useRef<number | null>(null);

  const handleStructureChanged = useCallback(() => setAssistantNotice(null), []);
  const canvas = useTgStructureCanvas({
    structure,
    onStructureChanged: handleStructureChanged
  });

  const operationBusy = canvas.isBusy || Boolean(openingModuleId);

  useEffect(() => {
    return () => {
      if (restoreFocusFrameRef.current !== null) {
        window.cancelAnimationFrame(restoreFocusFrameRef.current);
        restoreFocusFrameRef.current = null;
      }
    };
  }, []);

  useImperativeHandle(
    forwardedRef,
    () => ({
      syncBeforeLeave: canvas.syncBeforeLeave
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
        ?.querySelector<HTMLElement>("button, textarea")
        ?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [openPanel]);

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
    // This destination imports only after App commits its guarded navigation.
    if (id === "monomerRetrosynthesis") {
      closePanel(false);
      onOpenModule(id);
      return;
    }
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

  function updateAssistantInput(value: string) {
    setAssistantInput(value);
    setAssistantNotice(null);
  }

  return (
    <div
      className="np-module-page np-structure-workbench"
      data-module="structure-workbench"
    >
      <ModulePageHeader actions={<BrowsingRecordingControls module="structureWorkbench" />}>结构工作台</ModulePageHeader>
      <div className="np-sw-page np-module-page-body">
        <div className="np-sw-layout">
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
              modulePanelRef={modulePanelRef}
              assistantPanelRef={assistantPanelRef}
              openingModuleId={openingModuleId}
              selectedModuleName={selectedModuleName}
              structureSmiles={structure.smiles}
              assistantInput={assistantInput}
              assistantNotice={assistantNotice}
              onClose={closePanel}
              onOpenExternal={(id, name) => void openExternalModule(id, name)}
              onAssistantInputChange={updateAssistantInput}
              onAssistantNew={() => {
                setAssistantInput("");
                setAssistantNotice(null);
              }}
              onAssistantSend={() => {
                if (!requestServiceAccess()) return;
                if (assistantInput.trim()) {
                  setAssistantNotice("AI 对话接口尚未接入，本次内容未发送。");
                }
              }}
            />
          </main>
        </div>
      </div>
    </div>
  );
});
