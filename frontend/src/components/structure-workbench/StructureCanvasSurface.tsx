import {
  Atom,
  Check,
  Copy,
  Eraser,
  ImagePlus,
  LoaderCircle,
  Orbit,
  PanelTopOpen,
  RefreshCcw,
  SlidersHorizontal,
  Sparkles
} from "lucide-react";
import type { ReactNode, RefObject } from "react";
import type { useTgStructureCanvas } from "../../hooks/useTgStructureCanvas";
import type { StructureWorkspaceContext } from "../../types";
import { StructurePreview3D } from "../StructurePreview3D";

export type StructureCanvasController = ReturnType<typeof useTgStructureCanvas>;
export type StructureUtilityPanel = "modules" | "assistant" | null;

type StructureCanvasSurfaceProps = {
  structure: StructureWorkspaceContext;
  canvas: StructureCanvasController;
  draft: string;
  draftDirty: boolean;
  draftError: string | null;
  hasSharedConflict: boolean;
  isMobile: boolean;
  hasMountedEditor: boolean;
  isCanvasExpanded: boolean;
  hasActivated3D: boolean;
  previewSvg: string | null;
  isPreviewLoading: boolean;
  previewError: string | null;
  openPanel: StructureUtilityPanel;
  moduleButtonRef: RefObject<HTMLButtonElement | null>;
  assistantButtonRef: RefObject<HTMLButtonElement | null>;
  operationBusy: boolean;
  onDraftChange: (value: string) => void;
  onApplyDraft: () => void;
  onUseLatestShared: () => void;
  onLoadExample: () => void;
  onImportFile: (file: File) => void;
  onClear: () => void;
  onSync: () => void;
  onToggle3D: () => void;
  onTogglePanel: (panel: Exclude<StructureUtilityPanel, null>) => void;
  onOpenCanvas: () => void;
  onCollapseCanvas: () => void;
};

function ToolButton({
  label,
  tool,
  icon,
  busy = false,
  active = false,
  disabled = false,
  danger = false,
  buttonRef,
  controls,
  onClick
}: {
  label: string;
  tool: string;
  icon: ReactNode;
  busy?: boolean;
  active?: boolean;
  disabled?: boolean;
  danger?: boolean;
  buttonRef?: RefObject<HTMLButtonElement | null>;
  controls?: string;
  onClick: () => void;
}) {
  return (
    <button
      ref={buttonRef}
      type="button"
      className={`np-sw-tool${active ? " is-active" : ""}${danger ? " is-danger" : ""}`}
      data-workbench-tool={tool}
      aria-label={label}
      title={label}
      aria-expanded={controls ? active : undefined}
      aria-controls={controls}
      disabled={disabled}
      onClick={onClick}
    >
      {busy ? <LoaderCircle className="np-sw-spin" aria-hidden="true" /> : icon}
      <span>{label}</span>
    </button>
  );
}

function TextPreview({
  svg,
  loading,
  error,
  hasStructure,
  onOpenCanvas
}: {
  svg: string | null;
  loading: boolean;
  error: string | null;
  hasStructure: boolean;
  onOpenCanvas: () => void;
}) {
  return (
    <div className="np-sw-text-preview" aria-label="二维结构摘要">
      <div className="np-sw-text-preview__visual">
        {loading ? (
          <span className="np-sw-text-preview__state">
            <LoaderCircle className="np-sw-spin" /> 正在生成 2D 摘要
          </span>
        ) : svg ? (
          <img
            src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`}
            alt="当前共享结构的二维摘要"
          />
        ) : (
          <span className="np-sw-text-preview__empty">
            <Atom aria-hidden="true" />
            {error || (hasStructure ? "暂时无法生成二维摘要" : "应用 SMILES 后显示二维摘要")}
          </span>
        )}
      </div>
      <div className="np-sw-text-preview__copy">
        <strong>文本优先模式</strong>
        <p>可直接应用 SMILES；需要精细编辑时再打开绘图画布。</p>
        <button type="button" className="np-sw-secondary-button" onClick={onOpenCanvas}>
          <PanelTopOpen aria-hidden="true" />
          打开绘图画布
        </button>
      </div>
    </div>
  );
}

export function StructureCanvasSurface({
  structure,
  canvas,
  draft,
  draftDirty,
  draftError,
  hasSharedConflict,
  isMobile,
  hasMountedEditor,
  isCanvasExpanded,
  hasActivated3D,
  previewSvg,
  isPreviewLoading,
  previewError,
  openPanel,
  moduleButtonRef,
  assistantButtonRef,
  operationBusy,
  onDraftChange,
  onApplyDraft,
  onUseLatestShared,
  onLoadExample,
  onImportFile,
  onClear,
  onSync,
  onToggle3D,
  onTogglePanel,
  onOpenCanvas,
  onCollapseCanvas
}: StructureCanvasSurfaceProps) {
  const showEditor = !isMobile || isCanvasExpanded;
  const hasStructure = Boolean(structure.smiles.trim());

  return (
    <section className="np-sw-surface" aria-label="结构编辑工作区">
      <div className="np-sw-accent" aria-hidden="true" />
      <input
        ref={canvas.fileInputRef}
        className="np-sw-visually-hidden"
        type="file"
        accept="image/*"
        aria-label="导入结构图片"
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file) onImportFile(file);
        }}
      />

      <header className="np-sw-commandbar" aria-label="结构工作台工具栏">
        <div className="np-sw-tool-group">
          <span>结构来源</span>
          <ToolButton
            label="加载示例"
            tool="load"
            busy={canvas.isLoadingStructure}
            icon={<Atom aria-hidden="true" />}
            disabled={operationBusy}
            onClick={onLoadExample}
          />
          <ToolButton
            label="导入图片"
            tool="import"
            busy={canvas.isImportingImage}
            icon={<ImagePlus aria-hidden="true" />}
            disabled={operationBusy || !hasMountedEditor}
            onClick={() => canvas.fileInputRef.current?.click()}
          />
        </div>

        <div className="np-sw-tool-group">
          <span>画布操作</span>
          <ToolButton
            label="清空"
            tool="clear"
            busy={canvas.isClearing}
            icon={<Eraser aria-hidden="true" />}
            disabled={operationBusy || (hasMountedEditor && !canvas.isEditorReady)}
            danger
            onClick={onClear}
          />
          <ToolButton
            label="生成 SMILES"
            tool="sync"
            busy={canvas.isSyncing}
            icon={<RefreshCcw aria-hidden="true" />}
            disabled={operationBusy || !hasMountedEditor || !canvas.isEditorReady}
            onClick={onSync}
          />
        </div>

        <div className="np-sw-tool-group">
          <span>视图</span>
          <ToolButton
            label={canvas.isFlipped ? "返回 2D" : "3D 构象"}
            tool="3d"
            busy={canvas.isFlipping}
            icon={<Orbit aria-hidden="true" />}
            disabled={operationBusy || !hasMountedEditor || !hasStructure}
            active={canvas.isFlipped}
            onClick={onToggle3D}
          />
          {isMobile && hasMountedEditor ? (
            <ToolButton
              label={isCanvasExpanded ? "收起画布" : "展开画布"}
              tool="canvas"
              icon={<PanelTopOpen aria-hidden="true" />}
              onClick={isCanvasExpanded ? onCollapseCanvas : onOpenCanvas}
            />
          ) : null}
        </div>

        <div className="np-sw-tool-group np-sw-tool-group--utility">
          <span>扩展功能</span>
          <ToolButton
            label="功能参数"
            tool="modules"
            icon={<SlidersHorizontal aria-hidden="true" />}
            active={openPanel === "modules"}
            buttonRef={moduleButtonRef}
            controls="structure-module-panel"
            onClick={() => onTogglePanel("modules")}
          />
          <ToolButton
            label="AI 助手"
            tool="assistant"
            icon={<Sparkles aria-hidden="true" />}
            active={openPanel === "assistant"}
            buttonRef={assistantButtonRef}
            controls="structure-assistant-panel"
            onClick={() => onTogglePanel("assistant")}
          />
        </div>
      </header>

      <div className="np-sw-canvas-stage">
        {!hasMountedEditor ? (
          <TextPreview
            svg={previewSvg}
            loading={isPreviewLoading}
            error={previewError}
            hasStructure={hasStructure}
            onOpenCanvas={onOpenCanvas}
          />
        ) : (
          <div className={`np-sw-editor${showEditor ? "" : " is-collapsed"}`}>
            <div
              className={`np-sw-editor__layer np-sw-editor__layer--2d${canvas.isFlipped ? " is-hidden" : " is-visible"}`}
              aria-hidden={canvas.isFlipped}
            >
              <iframe
                ref={structure.iframeRef}
                title="结构工作台结构编辑器"
                src="/ketcher/index.html"
                onLoad={canvas.handleEditorLoad}
              />
            </div>
            {hasActivated3D ? (
              <div
                className={`np-sw-editor__layer np-sw-editor__layer--3d${canvas.isFlipped ? " is-visible" : " is-hidden"}`}
                aria-hidden={!canvas.isFlipped}
              >
                <StructurePreview3D
                  smiles={structure.smiles}
                  variant="bare"
                  visualStyle="polished-atoms"
                  className="np-sw-preview-3d"
                  previewClassName="np-sw-preview-3d__frame"
                />
              </div>
            ) : null}
          </div>
        )}
      </div>

      <footer className="np-sw-smiles" aria-labelledby="np-sw-smiles-label">
        <div className="np-sw-smiles__heading">
          <div>
            <label id="np-sw-smiles-label" htmlFor="np-sw-smiles-input">SMILES</label>
            <span>{draftDirty ? "有未应用修改" : "与共享结构同步"}</span>
          </div>
          <span className={`np-sw-structure-state${hasStructure ? " is-ready" : ""}`}>
            <i aria-hidden="true" />
            {hasStructure ? "共享结构已就绪" : "尚未应用结构"}
          </span>
        </div>
        <div className={`np-sw-smiles__editor${draftError ? " is-invalid" : ""}`}>
          <textarea
            id="np-sw-smiles-input"
            rows={2}
            value={draft}
            onChange={(event) => onDraftChange(event.currentTarget.value)}
            placeholder="输入聚合物或单体 SMILES，确认后显式应用到共享结构。"
            spellCheck={false}
            aria-label="结构 SMILES 草稿"
            aria-describedby="np-sw-smiles-feedback"
          />
          <button
            type="button"
            className="np-sw-icon-button"
            onClick={() => void canvas.copySmiles(draft)}
            disabled={!draft.trim()}
            aria-label="复制 SMILES 草稿"
            title="复制 SMILES 草稿"
          >
            {canvas.copyState === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          </button>
          <button
            type="button"
            className="np-sw-primary-button"
            onClick={onApplyDraft}
            disabled={operationBusy || !draft.trim()}
          >
            {canvas.isLoadingStructure ? <LoaderCircle className="np-sw-spin" /> : <Sparkles />}
            应用结构
          </button>
        </div>
        <div id="np-sw-smiles-feedback" className="np-sw-smiles__feedback" aria-live="polite">
          <span className={draftError ? "is-error" : ""}>{draftError || canvas.feedback}</span>
          {hasSharedConflict ? (
            <button type="button" onClick={onUseLatestShared}>
              使用最新共享结构
            </button>
          ) : null}
        </div>
      </footer>
    </section>
  );
}
