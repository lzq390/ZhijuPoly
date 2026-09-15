import { BrowsingRecordingControls } from "./browsing-recording/BrowsingRecording";
import { ModulePageHeader } from "./ModulePageHeader";
import { SlidersHorizontal, Sparkles } from "lucide-react";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type CSSProperties
} from "react";
import { useConditionalGeneration } from "../hooks/useConditionalGeneration";
import { useConditionalGenerationStatus } from "../hooks/useConditionalGenerationStatus";
import { useTgStructureCanvas, wildcardCount } from "../hooks/useTgStructureCanvas";
import type {
  ConditionalGenerationTgRequest,
  StructureWorkspaceContext
} from "../types";
import "../styles/structure-workbench.css";
import "../styles/conditional-generation.css";
import type { StructureCanvasOwnerHandle } from "./StructureWorkbenchPage";
import { ConditionalGenerationDrawer } from "./conditional-generation/ConditionalGenerationDrawer";
import {
  ConditionalGenerationUtilityPanels,
  type ConditionalGenerationOpenPanel
} from "./conditional-generation/ConditionalGenerationUtilityPanels";
import { StructureCanvasSurface } from "./structure-workbench/StructureCanvasSurface";

type ConditionalGenerationPageProps = {
  structure: StructureWorkspaceContext;
};

const CONDITIONAL_GENERATION_EXAMPLE_SMILES = "*CC*";

function validateRequest(request: ConditionalGenerationTgRequest) {
  if (!Number.isFinite(request.delta_tg)) {
    return "相对 Tg 变化必须为有效数值。";
  }
  if (
    !Number.isInteger(request.candidate_count) ||
    request.candidate_count < 1 ||
    request.candidate_count > 50
  ) {
    return "候选数量必须为 1–50 的整数。";
  }
  if (!Number.isInteger(request.top_k) || request.top_k < 1 || request.top_k > 20) {
    return "Top-K 必须为 1–20 的整数。";
  }
  if (
    !Number.isFinite(request.temperature) ||
    request.temperature < 0.1 ||
    request.temperature > 2
  ) {
    return "Temperature 必须在 0.1–2.0 之间。";
  }
  return null;
}

function requestsDiffer(
  draft: ConditionalGenerationTgRequest,
  submitted: ConditionalGenerationTgRequest | null
) {
  if (!submitted) return false;
  return (
    draft.delta_tg !== submitted.delta_tg ||
    draft.candidate_count !== submitted.candidate_count ||
    draft.top_k !== submitted.top_k ||
    draft.temperature !== submitted.temperature
  );
}

export const ConditionalGenerationPage = forwardRef<
  StructureCanvasOwnerHandle,
  ConditionalGenerationPageProps
>(function ConditionalGenerationPage({ structure }, forwardedRef) {
  const generation = useConditionalGeneration();
  const {
    serviceStatus,
    serviceStatusError,
    isStatusLoading,
    refreshStatus
  } = useConditionalGenerationStatus();
  const [openPanel, setOpenPanel] = useState<ConditionalGenerationOpenPanel>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [hasActivated3D, setHasActivated3D] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [hasRun, setHasRun] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [structureError, setStructureError] = useState<string | null>(null);
  const [assistantInput, setAssistantInput] = useState("");
  const [assistantNotice, setAssistantNotice] = useState<string | null>(null);
  const parameterPanelRef = useRef<HTMLElement | null>(null);
  const assistantPanelRef = useRef<HTMLElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const assistantButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusFrameRef = useRef<number | null>(null);

  const handleStructureChanged = useCallback(() => {
    generation.reset();
    setHasRun(false);
    setDrawerOpen(false);
    setStructureError(null);
  }, [generation]);

  const canvas = useTgStructureCanvas({
    structure,
    onStructureChanged: handleStructureChanged
  });

  const closePanel = useCallback((restoreFocus = true) => {
    const panel = openPanel;
    setOpenPanel(null);
    if (!panel || !restoreFocus) return;
    if (restoreFocusFrameRef.current !== null) {
      window.cancelAnimationFrame(restoreFocusFrameRef.current);
    }
    restoreFocusFrameRef.current = window.requestAnimationFrame(() => {
      restoreFocusFrameRef.current = null;
      (panel === "parameters" ? parameterButtonRef.current : assistantButtonRef.current)?.focus();
    });
  }, [openPanel]);

  useEffect(() => {
    return () => {
      if (restoreFocusFrameRef.current !== null) {
        window.cancelAnimationFrame(restoreFocusFrameRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!openPanel) return;
    const panel = openPanel === "parameters" ? parameterPanelRef.current : assistantPanelRef.current;
    const frame = window.requestAnimationFrame(() => {
      panel
        ?.querySelector<HTMLElement>(
          openPanel === "parameters" ? ".np-cg-field input" : ".np-sw-assistant-composer textarea"
        )
        ?.focus();
    });

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      const trigger = openPanel === "parameters" ? parameterButtonRef.current : assistantButtonRef.current;
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
      window.cancelAnimationFrame(frame);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [closePanel, openPanel]);

  useImperativeHandle(
    forwardedRef,
    () => ({
      syncBeforeLeave: canvas.syncBeforeLeave
    }),
    [canvas]
  );

  const validationMessage = validateRequest(generation.request);
  const parametersDirty = requestsDiffer(generation.request, generation.submittedRequest);
  const operationBusy = canvas.isBusy || generation.isLoading;
  const serviceReady = serviceStatus?.available === true;
  const resultCount = generation.data?.returned_count ?? generation.job?.accepted_count ?? 0;
  const resultStatus = generation.isLoading
    ? generation.job?.status === "pending"
      ? "任务排队中"
      : "候选生成中"
    : generation.error
      ? "生成需要检查"
      : generation.data
        ? `${resultCount} 个候选`
        : "尚未生成";

  function updateRequest(partial: Partial<ConditionalGenerationTgRequest>) {
    setStructureError(null);
    generation.setRequest({ ...generation.request, ...partial });
  }

  function togglePanel(panel: Exclude<ConditionalGenerationOpenPanel, null>) {
    if (openPanel === panel) closePanel(true);
    else setOpenPanel(panel);
  }

  async function toggle3D() {
    const activating = !canvas.isFlipped;
    const changed = await canvas.toggle3D();
    if (changed && activating) setHasActivated3D(true);
  }

  async function handleGenerate() {
    setStructureError(null);
    if (validationMessage || operationBusy || !serviceReady) return;

    const smiles = await canvas.resolveSmilesForSearch();
    if (!smiles) {
      setStructureError("请先在结构画布中绘制或输入种子结构。");
      setOpenPanel("parameters");
      return;
    }
    if (wildcardCount(smiles) < 2) {
      setStructureError("种子聚合物必须包含至少两个 * 连接点。");
      setOpenPanel("parameters");
      return;
    }

    const request: ConditionalGenerationTgRequest = {
      ...generation.request,
      smiles
    };
    setHasRun(true);
    setOpenPanel(null);
    setDrawerOpen(true);
    void generation.submit(request);
  }

  function handleAssistantSend() {
    if (!assistantInput.trim()) return;
    setAssistantNotice("AI 对话接口尚未接入，本次内容未发送。");
  }

  function parameterStatusText() {
    if (structureError) return structureError;
    if (validationMessage) return validationMessage;
    if (isStatusLoading) return "正在检查条件生成服务…";
    if (serviceStatusError) return `服务检查失败：${serviceStatusError}`;
    if (serviceStatus && !serviceStatus.available) {
      return serviceStatus.enabled
        ? "生成模型文件不完整，请检查部署配置。"
        : "条件生成服务当前未启用。";
    }
    if (parametersDirty) return "参数已修改，需要重新生成。";
    return "参数已就绪 · ΔTg 是相对种子结构的变化条件。";
  }

  const parameterHasError = Boolean(
    structureError ||
    validationMessage ||
    serviceStatusError ||
    (serviceStatus && !serviceStatus.available)
  );
  const workbenchStyle = { "--np-sw-drawer-width": `${drawerWidth}px` } as CSSProperties;

  return (
    <div
      className="np-module-page np-structure-workbench np-conditional-generation"
      data-module="conditional-generation"
      style={workbenchStyle}
    >
      <ModulePageHeader actions={<BrowsingRecordingControls module="conditionalGeneration" />}>条件聚合物生成</ModulePageHeader>
      <div className={`np-sw-page np-module-page-body${drawerOpen ? " has-open-drawer" : ""}`}>
        <div className={`np-sw-layout${drawerOpen ? " has-open-drawer" : ""}`}>
          <main className="np-sw-workspace">
            <StructureCanvasSurface
              structure={structure}
              canvas={canvas}
              hasActivated3D={hasActivated3D}
              operationBusy={operationBusy}
              editorTitle="条件聚合物生成结构编辑器"
              utilityActions={[
                {
                  id: "generation-parameters",
                  label: "生成参数",
                  icon: <SlidersHorizontal aria-hidden="true" />,
                  active: openPanel === "parameters",
                  buttonRef: parameterButtonRef,
                  controls: "cg-parameter-panel",
                  onClick: () => togglePanel("parameters")
                },
                {
                  id: "generation-assistant",
                  label: "AI 助手",
                  icon: <Sparkles aria-hidden="true" />,
                  active: openPanel === "assistant",
                  buttonRef: assistantButtonRef,
                  controls: "cg-assistant-panel",
                  onClick: () => togglePanel("assistant")
                }
              ]}
              onLoadExample={() => canvas.loadStructure(CONDITIONAL_GENERATION_EXAMPLE_SMILES)}
              onImportFile={(file) => canvas.importImageFile(file)}
              onClear={() => canvas.clearCanvas()}
              onSync={() => canvas.syncSmilesFromCanvas()}
              onToggle3D={toggle3D}
            />

            <ConditionalGenerationUtilityPanels
              openPanel={openPanel}
              parameterPanelRef={parameterPanelRef}
              assistantPanelRef={assistantPanelRef}
              request={generation.request}
              advancedOpen={advancedOpen}
              parameterStatus={parameterStatusText()}
              parameterHasError={parameterHasError}
              serviceNeedsRefresh={Boolean(serviceStatusError || (serviceStatus && !serviceStatus.available))}
              isStatusLoading={isStatusLoading}
              submitting={generation.isLoading}
              canSubmit={!validationMessage && !operationBusy && serviceReady}
              structureSmiles={structure.smiles}
              resultStatus={resultStatus}
              assistantInput={assistantInput}
              assistantNotice={assistantNotice}
              onClose={closePanel}
              onAdvancedOpenChange={setAdvancedOpen}
              onRequestChange={updateRequest}
              onRefreshStatus={() => void refreshStatus()}
              onSubmit={() => void handleGenerate()}
              onAssistantInputChange={(value) => {
                setAssistantInput(value);
                setAssistantNotice(null);
              }}
              onAssistantNew={() => {
                setAssistantInput("");
                setAssistantNotice(null);
              }}
              onAssistantSend={handleAssistantSend}
            />
          </main>

          <ConditionalGenerationDrawer
            open={drawerOpen}
            hasRun={hasRun}
            width={drawerWidth}
            loading={generation.isLoading}
            error={generation.error}
            data={generation.data}
            job={generation.job}
            onWidthChange={setDrawerWidth}
            onClose={() => setDrawerOpen(false)}
            onOpen={() => setDrawerOpen(true)}
          />
        </div>
      </div>
    </div>
  );
});
