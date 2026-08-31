import {
  Atom,
  Box,
  Check,
  Copy,
  Eraser,
  ImagePlus,
  LoaderCircle,
  RefreshCcw
} from "lucide-react";
import type { ReactNode, RefObject } from "react";
import type { useTgStructureCanvas } from "../../hooks/useTgStructureCanvas";
import type { StructureWorkspaceContext } from "../../types";
import { StructurePreview3D } from "../StructurePreview3D";

export type StructureCanvasController = ReturnType<typeof useTgStructureCanvas>;
export type StructureUtilityPanel = "modules" | "assistant" | null;
export type StructureCanvasUtilityAction = {
  id: string;
  label: string;
  icon: ReactNode;
  active?: boolean;
  busy?: boolean;
  disabled?: boolean;
  controls?: string;
  buttonRef?: RefObject<HTMLButtonElement | null>;
  onClick: () => void;
};

type StructureCanvasSurfaceProps = {
  structure: StructureWorkspaceContext;
  canvas: StructureCanvasController;
  hasActivated3D: boolean;
  operationBusy: boolean;
  editorTitle?: string;
  utilityActions?: readonly StructureCanvasUtilityAction[];
  onLoadExample: () => void;
  onImportFile: (file: File) => void;
  onClear: () => void;
  onSync: () => void;
  onToggle3D: () => void;
};

function ToolButton({
  label,
  tool,
  icon,
  busy = false,
  active = false,
  disabled = false,
  danger = false,
  primary = false,
  iconOnly = false,
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
  primary?: boolean;
  iconOnly?: boolean;
  buttonRef?: RefObject<HTMLButtonElement | null>;
  controls?: string;
  onClick: () => void;
}) {
  return (
    <button
      ref={buttonRef}
      type="button"
      className={`np-sw-tool${active ? " is-active" : ""}${danger ? " is-danger" : ""}${primary ? " is-primary" : ""}${iconOnly ? " np-sw-tool--icon" : ""}`}
      data-workbench-tool={tool}
      aria-label={label}
      title={label}
      aria-pressed={tool === "3d" ? active : undefined}
      aria-expanded={controls ? active : undefined}
      aria-controls={controls}
      disabled={disabled}
      onClick={onClick}
    >
      {busy ? <LoaderCircle className="np-sw-spin" aria-hidden="true" /> : icon}
      {iconOnly ? null : <span>{label}</span>}
    </button>
  );
}

export function StructureCanvasSurface({
  structure,
  canvas,
  hasActivated3D,
  operationBusy,
  editorTitle = "结构工作台结构编辑器",
  utilityActions = [],
  onLoadExample,
  onImportFile,
  onClear,
  onSync,
  onToggle3D
}: StructureCanvasSurfaceProps) {
  return (
    <section className="np-sw-surface" aria-label="结构编辑工作区">
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
        <div className="np-sw-toolbar">
          <ToolButton
            label="加载结构"
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
            disabled={operationBusy}
            onClick={() => canvas.fileInputRef.current?.click()}
          />
          <ToolButton
            label="清空画布"
            tool="clear"
            busy={canvas.isClearing}
            icon={<Eraser aria-hidden="true" />}
            disabled={operationBusy || !canvas.isEditorReady}
            danger
            onClick={onClear}
          />
          <ToolButton
            label="生成SMILES"
            tool="sync"
            busy={canvas.isSyncing}
            icon={<RefreshCcw aria-hidden="true" />}
            disabled={operationBusy || !canvas.isEditorReady}
            primary
            onClick={onSync}
          />
          <ToolButton
            label={canvas.isFlipped ? "2D画布" : "3D构象"}
            tool="3d"
            busy={canvas.isFlipping}
            icon={<Box aria-hidden="true" />}
            disabled={operationBusy || !canvas.isEditorReady}
            active={canvas.isFlipped}
            onClick={onToggle3D}
          />
          {utilityActions.length ? <span className="np-sw-toolbar-separator" aria-hidden="true" /> : null}
          {utilityActions.map((action) => (
            <ToolButton
              key={action.id}
              label={action.label}
              tool={action.id}
              icon={action.icon}
              active={Boolean(action.active)}
              busy={Boolean(action.busy)}
              disabled={Boolean(action.disabled)}
              iconOnly
              buttonRef={action.buttonRef}
              controls={action.controls}
              onClick={action.onClick}
            />
          ))}
        </div>
      </header>

      <div className="np-sw-canvas-stage">
        <div className={`np-sw-editor${canvas.isFlipped ? " is-flipped" : ""}`}>
          <div
            className={`np-sw-editor__layer np-sw-editor__layer--2d${canvas.isFlipped ? " is-hidden" : " is-visible"}`}
            aria-hidden={canvas.isFlipped}
            inert={canvas.isFlipped}
          >
            <iframe
              ref={structure.iframeRef}
              title={editorTitle}
              src="/ketcher/index.html"
              onLoad={canvas.handleEditorLoad}
            />
          </div>
          {hasActivated3D ? (
            <div
              className={`np-sw-editor__layer np-sw-editor__layer--3d${canvas.isFlipped ? " is-visible" : " is-hidden"}`}
              aria-hidden={!canvas.isFlipped}
              inert={!canvas.isFlipped}
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
      </div>

      <footer className="np-sw-smiles" aria-labelledby="np-sw-smiles-label">
        <label id="np-sw-smiles-label">SMILES</label>
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
          {canvas.copyState === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
        </button>
        {canvas.feedback ? <p role="status" aria-live="polite">{canvas.feedback}</p> : null}
      </footer>
    </section>
  );
}
