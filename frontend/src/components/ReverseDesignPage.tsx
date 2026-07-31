import {
  ArrowUp,
  Box,
  Check,
  Copy,
  Eraser,
  ImagePlus,
  LoaderCircle,
  MessageSquareText,
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
import { useReverseDesign } from "../hooks/useReverseDesign";
import { useTgStructureCanvas } from "../hooks/useTgStructureCanvas";
import type {
  KnowledgeNavigationRequest,
  ReverseDesignTgRequest,
  StructureWorkspaceContext
} from "../types";
import { ReverseDesignResults } from "./ReverseDesignResults";
import { StructurePreview3D } from "./StructurePreview3D";
import "../styles/polymer-desktop.css";
import "../styles/reverse-design.css";

type ReverseDesignPageProps = {
  structure: StructureWorkspaceContext;
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
};

type OpenPanel = "parameters" | "assistant" | null;

const DRAWER_MIN_WIDTH = 320;
const DRAWER_MAX_WIDTH = 560;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function validateRequest(request: ReverseDesignTgRequest) {
  if (
    request.target_tg === null ||
    !Number.isFinite(request.target_tg)
  ) {
    return "目标 Tg 必须为有效数值。";
  }
  if (
    !Number.isFinite(request.similarity_threshold) ||
    request.similarity_threshold < 0 ||
    request.similarity_threshold > 1
  ) {
    return "相似度阈值必须在 0–1 之间。";
  }
  if (
    !Number.isInteger(request.candidate_size) ||
    request.candidate_size < 1 ||
    request.candidate_size > 200
  ) {
    return "候选数量必须为 1–200 的整数。";
  }
  return null;
}

function requestsDiffer(
  draft: ReverseDesignTgRequest,
  submitted: ReverseDesignTgRequest | null
) {
  if (!submitted) {
    return false;
  }
  return (
    draft.target_tg !== submitted.target_tg ||
    draft.similarity_threshold !== submitted.similarity_threshold ||
    draft.candidate_size !== submitted.candidate_size
  );
}

export function ReverseDesignPage({
  structure,
  onOpenKnowledge
}: ReverseDesignPageProps) {
  const reverseDesign = useReverseDesign();
  const [openPanel, setOpenPanel] = useState<OpenPanel>(null);
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [hasRun, setHasRun] = useState(false);
  const [drawerWidth, setDrawerWidth] = useState(380);
  const [assistantInput, setAssistantInput] = useState("");
  const [assistantNotice, setAssistantNotice] = useState<string | null>(null);
  const parameterPanelRef = useRef<HTMLElement | null>(null);
  const assistantPanelRef = useRef<HTMLElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const assistantButtonRef = useRef<HTMLButtonElement | null>(null);
  const resizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const handleStructureChanged = useCallback(() => {
    reverseDesign.reset();
    setHasRun(false);
    setIsDrawerOpen(false);
  }, [reverseDesign]);

  const canvas = useTgStructureCanvas({
    structure,
    onStructureChanged: handleStructureChanged
  });

  const validationMessage = validateRequest(reverseDesign.request);
  const parametersDirty = requestsDiffer(
    reverseDesign.request,
    reverseDesign.submittedRequest
  );
  const operationBusy = canvas.isBusy || reverseDesign.isLoading;
  const resultCount = reverseDesign.data?.total ?? reverseDesign.job?.matched_count ?? 0;
  const resultStatus = reverseDesign.isLoading
    ? "候选搜索中"
    : reverseDesign.error
      ? "搜索需要检查"
      : reverseDesign.data
        ? `${resultCount} 个候选`
        : "尚未搜索";

  function updateRequest(partial: Partial<ReverseDesignTgRequest>) {
    reverseDesign.setRequest({
      ...reverseDesign.request,
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

  async function handleSearch() {
    if (validationMessage || operationBusy) {
      return;
    }
    const smiles = await canvas.resolveSmilesForSearch();
    if (!smiles) {
      setOpenPanel("parameters");
      return;
    }

    const request: ReverseDesignTgRequest = {
      ...reverseDesign.request,
      smiles
    };
    setHasRun(true);
    openDrawer();
    void reverseDesign.submit(request);
  }

  function handleAssistantSend() {
    if (!assistantInput.trim()) {
      return;
    }
    setAssistantNotice("AI 对话接口尚未接入，本次内容未发送。");
  }

  const rootStyle = {
    "--tg-drawer-width": `${drawerWidth}px`
  } as CSSProperties;

  return (
    <div
      className={`polymer-desktop-page polymer-desktop-page--embedded tg-reverse-page${isDrawerOpen ? " has-open-drawer" : ""}`}
      style={rootStyle}
    >
      <h1 className="tg-page-title">Tg 逆向设计</h1>

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
            <div className="header-actions tg-toolbar" aria-label="Tg 结构工具栏">
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
                aria-label="搜索参数"
                title="搜索参数"
                aria-expanded={openPanel === "parameters"}
                aria-controls="tg-parameter-panel"
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
                aria-controls="tg-assistant-panel"
                onClick={() => togglePanel("assistant")}
              >
                <Sparkles />
              </button>
            </div>

            <section
              ref={parameterPanelRef}
              id="tg-parameter-panel"
              className={`tg-parameter-panel${openPanel === "parameters" ? " is-open" : ""}`}
              role="dialog"
              aria-modal="false"
              aria-labelledby="tg-parameter-title"
              aria-hidden={openPanel !== "parameters"}
              inert={openPanel !== "parameters"}
            >
              <header>
                <h2 id="tg-parameter-title">Tg 搜索参数</h2>
                <button type="button" aria-label="收起搜索参数" onClick={() => closePanel()}>
                  <X />
                </button>
              </header>
              <div className="tg-parameter-fields">
                <label>
                  <span>目标 Tg</span>
                  <span className="tg-input-shell">
                    <input
                      type="number"
                      step="0.1"
                      value={reverseDesign.request.target_tg ?? ""}
                      onChange={(event) =>
                        updateRequest({
                          target_tg:
                            event.currentTarget.value === ""
                              ? null
                              : Number(event.currentTarget.value)
                        })
                      }
                    />
                    <small>°C</small>
                  </span>
                </label>
                <label>
                  <span>相似度阈值</span>
                  <span className="tg-input-shell">
                    <input
                      type="number"
                      min="0"
                      max="1"
                      step="0.01"
                      value={
                        Number.isNaN(reverseDesign.request.similarity_threshold)
                          ? ""
                          : reverseDesign.request.similarity_threshold
                      }
                      onChange={(event) =>
                        updateRequest({
                          similarity_threshold:
                            event.currentTarget.value === ""
                              ? Number.NaN
                              : Number(event.currentTarget.value)
                        })
                      }
                    />
                  </span>
                </label>
                <label>
                  <span>候选数量</span>
                  <span className="tg-input-shell">
                    <input
                      type="number"
                      min="1"
                      max="200"
                      step="1"
                      value={
                        Number.isNaN(reverseDesign.request.candidate_size)
                          ? ""
                          : reverseDesign.request.candidate_size
                      }
                      onChange={(event) =>
                        updateRequest({
                          candidate_size:
                            event.currentTarget.value === ""
                              ? Number.NaN
                              : Number(event.currentTarget.value)
                        })
                      }
                    />
                    <small>个</small>
                  </span>
                </label>
              </div>
              <div className="tg-parameter-validation" role="status" aria-live="polite">
                {validationMessage ||
                  (parametersDirty ? "参数已修改，需要重新搜索。" : "参数已就绪。")}
              </div>
              <button
                type="button"
                className="tg-search-button"
                onClick={() => void handleSearch()}
                disabled={Boolean(validationMessage) || operationBusy}
              >
                {reverseDesign.isLoading ? <LoaderCircle className="animate-spin" /> : <Search />}
                搜索
              </button>
            </section>

            <section
              ref={assistantPanelRef}
              id="tg-assistant-panel"
              className={`tg-assistant-panel${openPanel === "assistant" ? " is-open" : ""}`}
              role="dialog"
              aria-modal="false"
              aria-labelledby="tg-assistant-title"
              aria-hidden={openPanel !== "assistant"}
              inert={openPanel !== "assistant"}
            >
              <header className="tg-assistant-header">
                <div>
                  <span className="tg-assistant-mark"><Sparkles /></span>
                  <span>
                    <h2 id="tg-assistant-title">Tg AI 助手</h2>
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
                  <span>{`Tg ${reverseDesign.request.target_tg ?? "—"} °C`}</span>
                  <span>{resultStatus}</span>
                </div>

                <div className="tg-assistant-welcome">
                  <span className="tg-assistant-orb"><Sparkles /></span>
                  <h3><em>你好，</em><br />今天想一起研究什么？</h3>
                  <p>我会结合当前共享结构、Tg 搜索参数和候选结果辅助分析。</p>
                  <div className="tg-assistant-suggestions">
                    {[
                      "解释当前结构对 Tg 的影响",
                      "建议更合适的搜索参数",
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

          <section className="tg-structure-surface" aria-label="结构画布">
            <div className={`tg-structure-flip${canvas.isFlipped ? " is-flipped" : ""}`}>
              <div className="tg-structure-face tg-structure-face-front">
                <iframe
                  ref={structure.iframeRef}
                  title="Tg 逆向设计结构编辑器"
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

          <section className="tg-smiles-capsule" aria-labelledby="tg-smiles-label">
            <label id="tg-smiles-label">SMILES</label>
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
              <p role="status" aria-live="polite">{canvas.feedback}</p>
            ) : null}
          </section>
        </div>
      </div>

      <aside
        className={`tg-results-drawer${isDrawerOpen ? " is-open" : ""}`}
        aria-hidden={!isDrawerOpen}
        inert={!isDrawerOpen}
        aria-labelledby="tg-results-title"
      >
        <div
          className="tg-drawer-resizer"
          role="separator"
          tabIndex={isDrawerOpen ? 0 : -1}
          aria-label="调整候选结果抽屉宽度"
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
            <span><MessageSquareText /></span>
            <div>
              <h2 id="tg-results-title">Tg 候选结果</h2>
              <p>{resultStatus}</p>
            </div>
          </div>
          <button
            type="button"
            aria-label="关闭候选结果"
            onClick={() => setIsDrawerOpen(false)}
          >
            <X />
          </button>
        </header>
        <div className="tg-results-body">
          <ReverseDesignResults
            data={reverseDesign.data}
            error={reverseDesign.error}
            isLoading={reverseDesign.isLoading}
            job={reverseDesign.job}
            submittedRequest={reverseDesign.submittedRequest}
            onOpenKnowledge={onOpenKnowledge}
          />
        </div>
      </aside>

      {hasRun && !isDrawerOpen ? (
        <button
          type="button"
          className="btn-expand-analysis tg-drawer-reopen"
          onClick={openDrawer}
          aria-label="展开 Tg 候选结果"
          title="展开 Tg 候选结果"
        >
          <Search width={14} height={14} />
        </button>
      ) : null}
    </div>
  );
}
