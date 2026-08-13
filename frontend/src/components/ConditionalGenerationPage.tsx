import {
  ArrowUp,
  Atom,
  Box,
  Check,
  ChevronDown,
  Copy,
  Eraser,
  ImagePlus,
  LoaderCircle,
  Plus,
  RefreshCcw,
  Search,
  SlidersHorizontal,
  Sparkles,
  X
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent
} from "react";
import { useConditionalGeneration } from "../hooks/useConditionalGeneration";
import { useConditionalGenerationStatus } from "../hooks/useConditionalGenerationStatus";
import { useTgStructureCanvas, wildcardCount } from "../hooks/useTgStructureCanvas";
import type {
  ConditionalGenerationTgRequest,
  StructureWorkspaceContext
} from "../types";
import { ConditionalGenerationResults } from "./ConditionalGenerationResults";
import { StructurePreview3D } from "./StructurePreview3D";
import "../styles/polymer-desktop.css";
import "../styles/reverse-design.css";

type ConditionalGenerationPageProps = {
  structure: StructureWorkspaceContext;
};

type OpenPanel = "parameters" | "assistant" | null;

const DRAWER_MIN_WIDTH = 320;
const DRAWER_MAX_WIDTH = 560;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

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
  if (
    !Number.isInteger(request.top_k) ||
    request.top_k < 1 ||
    request.top_k > 20
  ) {
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
  if (!submitted) {
    return false;
  }
  return (
    draft.delta_tg !== submitted.delta_tg ||
    draft.candidate_count !== submitted.candidate_count ||
    draft.top_k !== submitted.top_k ||
    draft.temperature !== submitted.temperature
  );
}

function useMobileDrawer() {
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia("(max-width: 899px)");
    const update = () => setIsMobile(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  return isMobile;
}

export function ConditionalGenerationPage({ structure }: ConditionalGenerationPageProps) {
  const generation = useConditionalGeneration();
  const {
    serviceStatus,
    serviceStatusError,
    isStatusLoading,
    refreshStatus
  } = useConditionalGenerationStatus();
  const [openPanel, setOpenPanel] = useState<OpenPanel>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [hasRun, setHasRun] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [structureError, setStructureError] = useState<string | null>(null);
  const [assistantInput, setAssistantInput] = useState("");
  const [assistantNotice, setAssistantNotice] = useState<string | null>(null);
  const parameterPanelRef = useRef<HTMLElement | null>(null);
  const assistantPanelRef = useRef<HTMLElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const assistantButtonRef = useRef<HTMLButtonElement | null>(null);
  const drawerRef = useRef<HTMLElement | null>(null);
  const drawerCloseButtonRef = useRef<HTMLButtonElement | null>(null);
  const drawerReopenButtonRef = useRef<HTMLButtonElement | null>(null);
  const resizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const isMobileDrawer = useMobileDrawer();

  const handleStructureChanged = useCallback(() => {
    generation.reset();
    setHasRun(false);
    setIsDrawerOpen(false);
    setStructureError(null);
  }, [generation]);

  const canvas = useTgStructureCanvas({
    structure,
    onStructureChanged: handleStructureChanged
  });

  const validationMessage = validateRequest(generation.request);
  const parametersDirty = requestsDiffer(
    generation.request,
    generation.submittedRequest
  );
  const operationBusy = canvas.isBusy || generation.isLoading;
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
  const serviceReady = serviceStatus?.available === true;

  function updateRequest(partial: Partial<ConditionalGenerationTgRequest>) {
    setStructureError(null);
    generation.setRequest({
      ...generation.request,
      ...partial
    });
  }

  function restorePanelFocus(panel: Exclude<OpenPanel, null>) {
    const target =
      panel === "parameters" ? parameterButtonRef.current : assistantButtonRef.current;
    window.requestAnimationFrame(() => target?.focus());
  }

  function closePanel(restoreFocus = true) {
    if (openPanel && restoreFocus) {
      restorePanelFocus(openPanel);
    }
    setOpenPanel(null);
  }

  function togglePanel(panel: Exclude<OpenPanel, null>) {
    setOpenPanel((current) => (current === panel ? null : panel));
  }

  function openDrawer() {
    setOpenPanel(null);
    setIsDrawerOpen(true);
  }

  function closeDrawer(restoreFocus = true) {
    setIsDrawerOpen(false);
    if (restoreFocus) {
      window.requestAnimationFrame(() => drawerReopenButtonRef.current?.focus());
    }
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
        openPanel === "parameters" ? parameterPanelRef.current : assistantPanelRef.current;
      const trigger =
        openPanel === "parameters" ? parameterButtonRef.current : assistantButtonRef.current;
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
    if (!isDrawerOpen) {
      return;
    }

    if (isMobileDrawer) {
      window.requestAnimationFrame(() => drawerCloseButtonRef.current?.focus());
    }

    function handleDrawerKeyDown(event: KeyboardEvent) {
      if (openPanel) {
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        closeDrawer(true);
        return;
      }
      if (event.key !== "Tab" || !isMobileDrawer || !drawerRef.current) {
        return;
      }
      const focusable = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(
          'button:not(:disabled), [href], input:not(:disabled), [tabindex]:not([tabindex="-1"])'
        )
      ).filter((element) => element.getClientRects().length > 0);
      if (focusable.length === 0) {
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleDrawerKeyDown);
    return () => document.removeEventListener("keydown", handleDrawerKeyDown);
  }, [isDrawerOpen, isMobileDrawer, openPanel]);

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
    if (isMobileDrawer) {
      return;
    }
    event.preventDefault();
    resizeStateRef.current = {
      startX: event.clientX,
      startWidth: drawerWidth
    };
    document.body.classList.add("tg-is-resizing");
  }

  async function handleGenerate() {
    setStructureError(null);
    if (validationMessage || operationBusy || !serviceReady) {
      return;
    }

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
    openDrawer();
    void generation.submit(request);
  }

  function handleAssistantSend() {
    if (!assistantInput.trim()) {
      return;
    }
    setAssistantNotice("AI 对话接口尚未接入，本次内容未发送。");
  }

  function parameterStatusText() {
    if (structureError) {
      return structureError;
    }
    if (validationMessage) {
      return validationMessage;
    }
    if (isStatusLoading) {
      return "正在检查条件生成服务…";
    }
    if (serviceStatusError) {
      return `服务检查失败：${serviceStatusError}`;
    }
    if (serviceStatus && !serviceStatus.available) {
      return serviceStatus.enabled
        ? "生成模型文件不完整，请检查部署配置。"
        : "条件生成服务当前未启用。";
    }
    if (parametersDirty) {
      return "参数已修改，需要重新生成。";
    }
    return "参数已就绪 · ΔTg 是相对种子结构的变化条件。";
  }

  const parameterHasError = Boolean(
    structureError || validationMessage || serviceStatusError || (serviceStatus && !serviceStatus.available)
  );
  const rootStyle = {
    "--tg-drawer-width": `${drawerWidth}px`
  } as CSSProperties;

  return (
    <div
      className={`polymer-desktop-page polymer-desktop-page--embedded tg-reverse-page cg-generation-page${isDrawerOpen ? " has-open-drawer" : ""}`}
      style={rootStyle}
    >
      <h1 className="tg-page-title">条件聚合物生成</h1>

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
            <div className="header-actions tg-toolbar" aria-label="条件生成结构工具栏">
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button"
                id="btn-import-img"
                onClick={() => canvas.fileInputRef.current?.click()}
                disabled={operationBusy}
              >
                {canvas.isImportingImage ? <LoaderCircle className="animate-spin" /> : <ImagePlus />}
                导入图片
              </button>
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button"
                id="btn-clear-canvas"
                onClick={() => void canvas.clearCanvas()}
                disabled={operationBusy || !canvas.isEditorReady}
              >
                {canvas.isClearing ? <LoaderCircle className="animate-spin" /> : <Eraser />}
                清空画布
              </button>
              <button
                type="button"
                className="btn btn--outline btn--sm tg-tool-button"
                id="btn-sync-canvas"
                onClick={() => void canvas.syncSmilesFromCanvas()}
                disabled={operationBusy || !canvas.isEditorReady}
              >
                {canvas.isSyncing ? <LoaderCircle className="animate-spin" /> : <RefreshCcw />}
                生成SMILES
              </button>
              <button
                type="button"
                className={`btn btn--outline btn--sm tg-tool-button${canvas.isFlipped ? " active" : ""}`}
                id="btn-toggle-3d"
                onClick={() => void canvas.toggle3D()}
                disabled={operationBusy || !canvas.isEditorReady}
              >
                {canvas.isFlipping ? <LoaderCircle className="animate-spin" /> : <Box />}
                {canvas.isFlipped ? "2D画布" : "3D构象"}
              </button>
              <span className="tg-toolbar-separator" aria-hidden="true" />
              <button
                ref={parameterButtonRef}
                type="button"
                className={`btn btn--outline btn--sm tg-icon-tool${openPanel === "parameters" ? " is-active" : ""}`}
                aria-label="生成参数"
                title="生成参数"
                aria-expanded={openPanel === "parameters"}
                aria-controls="cg-parameter-panel"
                onClick={() => togglePanel("parameters")}
              >
                <SlidersHorizontal />
              </button>
              <button
                ref={assistantButtonRef}
                type="button"
                className={`btn btn--outline btn--sm tg-icon-tool${openPanel === "assistant" ? " is-active" : ""}`}
                aria-label="AI 助手"
                title="AI 助手"
                aria-expanded={openPanel === "assistant"}
                aria-controls="cg-assistant-panel"
                onClick={() => togglePanel("assistant")}
              >
                <Sparkles />
              </button>
            </div>

            <section
              ref={parameterPanelRef}
              id="cg-parameter-panel"
              className={`tg-parameter-panel cg-parameter-panel${openPanel === "parameters" ? " is-open" : ""}`}
              role="dialog"
              aria-modal="false"
              aria-labelledby="cg-parameter-title"
              aria-hidden={openPanel !== "parameters"}
              inert={openPanel !== "parameters"}
            >
              <header>
                <h2 id="cg-parameter-title">条件生成参数</h2>
                <button type="button" aria-label="收起生成参数" onClick={() => closePanel()}>
                  <X />
                </button>
              </header>
              <div className="tg-parameter-fields">
                <label>
                  <span>相对 Tg 变化</span>
                  <span className="tg-input-shell">
                    <input
                      type="number"
                      step="0.1"
                      value={Number.isNaN(generation.request.delta_tg) ? "" : generation.request.delta_tg}
                      onChange={(event) =>
                        updateRequest({
                          delta_tg: event.currentTarget.value === "" ? Number.NaN : Number(event.currentTarget.value)
                        })
                      }
                    />
                    <small>°C</small>
                  </span>
                </label>
                <label>
                  <span>候选数量</span>
                  <span className="tg-input-shell">
                    <input
                      type="number"
                      min="1"
                      max="50"
                      step="1"
                      value={Number.isNaN(generation.request.candidate_count) ? "" : generation.request.candidate_count}
                      onChange={(event) =>
                        updateRequest({
                          candidate_count: event.currentTarget.value === "" ? Number.NaN : Number(event.currentTarget.value)
                        })
                      }
                    />
                    <small>个</small>
                  </span>
                </label>

                <div className="cg-advanced-parameters">
                  <button
                    type="button"
                    className="cg-advanced-toggle"
                    aria-expanded={advancedOpen}
                    aria-controls="cg-advanced-fields"
                    onClick={() => setAdvancedOpen((current) => !current)}
                  >
                    <span>高级采样</span>
                    <ChevronDown aria-hidden="true" />
                  </button>
                  <div id="cg-advanced-fields" className="cg-advanced-fields" hidden={!advancedOpen}>
                    <label>
                      <span>Top-K</span>
                      <span className="tg-input-shell">
                        <input
                          type="number"
                          min="1"
                          max="20"
                          step="1"
                          value={Number.isNaN(generation.request.top_k) ? "" : generation.request.top_k}
                          onChange={(event) =>
                            updateRequest({
                              top_k: event.currentTarget.value === "" ? Number.NaN : Number(event.currentTarget.value)
                            })
                          }
                        />
                      </span>
                    </label>
                    <label>
                      <span>Temperature</span>
                      <span className="tg-input-shell">
                        <input
                          type="number"
                          min="0.1"
                          max="2"
                          step="0.05"
                          value={Number.isNaN(generation.request.temperature) ? "" : generation.request.temperature}
                          onChange={(event) =>
                            updateRequest({
                              temperature: event.currentTarget.value === "" ? Number.NaN : Number(event.currentTarget.value)
                            })
                          }
                        />
                      </span>
                    </label>
                  </div>
                </div>
              </div>
              <div
                className={`tg-parameter-validation${parameterHasError ? " is-error" : ""}`}
                role="status"
                aria-live="polite"
              >
                {parameterStatusText()}
              </div>
              {serviceStatusError || (serviceStatus && !serviceStatus.available) ? (
                <button
                  type="button"
                  className="cg-status-retry"
                  onClick={() => void refreshStatus()}
                  disabled={isStatusLoading}
                >
                  <RefreshCcw className={isStatusLoading ? "animate-spin" : ""} />
                  重新检查服务
                </button>
              ) : null}
              <button
                type="button"
                className="tg-search-button"
                onClick={() => void handleGenerate()}
                disabled={Boolean(validationMessage) || operationBusy || !serviceReady}
              >
                {generation.isLoading ? <LoaderCircle className="animate-spin" /> : <Search />}
                {generation.isLoading ? "生成中" : "运行生成"}
              </button>
            </section>

            <section
              ref={assistantPanelRef}
              id="cg-assistant-panel"
              className={`tg-assistant-panel${openPanel === "assistant" ? " is-open" : ""}`}
              role="dialog"
              aria-modal="false"
              aria-labelledby="cg-assistant-title"
              aria-hidden={openPanel !== "assistant"}
              inert={openPanel !== "assistant"}
            >
              <header className="tg-assistant-header">
                <div>
                  <span className="tg-assistant-mark"><Sparkles /></span>
                  <span>
                    <h2 id="cg-assistant-title">条件生成 AI 助手</h2>
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
                  <button type="button" aria-label="收起 AI 助手" onClick={() => closePanel()}>
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
                  <span>{`ΔTg ${Number.isFinite(generation.request.delta_tg) ? generation.request.delta_tg : "—"} °C`}</span>
                  <span>{resultStatus}</span>
                </div>

                <div className="tg-assistant-welcome">
                  <span className="tg-assistant-orb"><Sparkles /></span>
                  <h3><em>你好，</em><br />今天想一起研究什么？</h3>
                  <p>我会结合当前种子结构、相对 Tg 条件和候选结构辅助分析。</p>
                  <div className="tg-assistant-suggestions">
                    {[
                      "解释当前种子结构的生成空间",
                      "建议更合适的采样参数",
                      "比较候选结构的关键差异"
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
                    <span><Plus /> 科研助手</span>
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

          <section className="tg-structure-surface" aria-label="种子结构画布">
            <div className={`tg-structure-flip${canvas.isFlipped ? " is-flipped" : ""}`}>
              <div className="tg-structure-face tg-structure-face-front">
                <iframe
                  ref={structure.iframeRef}
                  title="条件聚合物生成结构编辑器"
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

          <section className="tg-smiles-capsule" aria-labelledby="cg-smiles-label">
            <label id="cg-smiles-label">SMILES</label>
            <textarea
              rows={2}
              readOnly
              value={structure.smiles}
              placeholder="在上方 Ketcher 画布绘制种子结构后，点击“生成SMILES”。"
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
            <p role="status" aria-live="polite">
              {canvas.feedback || "聚合物连接点保持为 *；仅 3D 空间预览时封氢。"}
            </p>
          </section>
        </div>
      </div>

      <button
        type="button"
        className={`cg-drawer-backdrop${isDrawerOpen ? " is-open" : ""}`}
        aria-label="关闭候选结果"
        tabIndex={isDrawerOpen && isMobileDrawer ? 0 : -1}
        onClick={() => closeDrawer(true)}
      />

      <aside
        ref={drawerRef}
        className={`tg-results-drawer cg-results-drawer${isDrawerOpen ? " is-open" : ""}`}
        role="dialog"
        aria-modal={isMobileDrawer}
        aria-hidden={!isDrawerOpen}
        inert={!isDrawerOpen}
        aria-labelledby="cg-results-title"
      >
        <div
          className="tg-drawer-resizer"
          role="separator"
          tabIndex={isDrawerOpen && !isMobileDrawer ? 0 : -1}
          aria-label="调整候选结果抽屉宽度"
          aria-orientation="vertical"
          aria-valuemin={DRAWER_MIN_WIDTH}
          aria-valuemax={DRAWER_MAX_WIDTH}
          aria-valuenow={Math.round(drawerWidth)}
          onPointerDown={startDrawerResize}
          onKeyDown={(event) => {
            const amount = event.shiftKey ? 40 : 16;
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
              event.preventDefault();
              setDrawerWidth((current) =>
                clamp(
                  current + (event.key === "ArrowLeft" ? amount : -amount),
                  DRAWER_MIN_WIDTH,
                  DRAWER_MAX_WIDTH
                )
              );
            } else if (event.key === "Home") {
              event.preventDefault();
              setDrawerWidth(DRAWER_MIN_WIDTH);
            } else if (event.key === "End") {
              event.preventDefault();
              setDrawerWidth(DRAWER_MAX_WIDTH);
            }
          }}
        />
        <header className="tg-results-header">
          <div>
            <span><Atom /></span>
            <div>
              <h2 id="cg-results-title">条件生成候选</h2>
              <p>{resultStatus}</p>
            </div>
          </div>
          <button
            ref={drawerCloseButtonRef}
            type="button"
            aria-label="关闭候选结果"
            onClick={() => closeDrawer(true)}
          >
            <X />
          </button>
        </header>
        <div className="tg-results-body">
          <ConditionalGenerationResults
            data={generation.data}
            error={generation.error}
            isLoading={generation.isLoading}
            job={generation.job}
          />
        </div>
      </aside>

      {hasRun && !isDrawerOpen ? (
        <button
          ref={drawerReopenButtonRef}
          type="button"
          className="btn-expand-analysis tg-drawer-reopen"
          onClick={openDrawer}
          aria-label="展开条件生成候选"
          title="展开条件生成候选"
        >
          <Search width={14} height={14} />
        </button>
      ) : null}
    </div>
  );
}
