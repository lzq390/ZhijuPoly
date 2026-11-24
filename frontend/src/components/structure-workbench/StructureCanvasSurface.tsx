import { CanvasLibraryActions } from "../../structure-library/CanvasLibraryActions";
import { requestServiceAccess } from "../../auth/guestAccess";
import {
  Atom,
  Box,
  Check,
  ChevronDown,
  Copy,
  Eraser,
  ImagePlus,
  LoaderCircle,
  RefreshCcw
} from "lucide-react";
import { type ReactNode, type RefObject } from "react";
import type { useTgStructureCanvas } from "../../hooks/useTgStructureCanvas";
import { usePopoverMenu } from "../../hooks/usePopoverMenu";
import type { StructureWorkspaceContext } from "../../types";
import { StructurePreview3D } from "../StructurePreview3D";
import { StructureEditor } from "./StructureEditor";

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
  /** Optional leading label for pages that frame the canvas as one zone of a larger surface. */
  commandbarLabel?: ReactNode;
  utilityActions?: readonly StructureCanvasUtilityAction[];
  onLoadExample: () => void | Promise<unknown>;
  onImportFile: (file: File) => void | Promise<unknown>;
  onClear: () => void | Promise<unknown>;
  onSync: () => void | Promise<unknown>;
  onToggle3D: () => void | Promise<unknown>;
  onSmilesDraftChange?: (value: string) => void;
  /** Optional guidance over an empty 2D canvas; `load` writes a structure through the normal mutation path. */
  emptyState?: (load: (smiles: string) => void) => ReactNode;
  /** Optional row under the SMILES field, inside the same surface. */
  footer?: ReactNode;
  /** Render 清空画布 after the other tools, so focus order matches a layout that shows it last. */
  clearLast?: boolean;
  /** "segmented" shows 2D and 3D side by side instead of one toggle button. */
  viewSwitch?: "toggle" | "segmented";
  /** When given, 加载示例 opens a menu of these structures instead of loading one example. */
  examples?: readonly StructureExample[];
};

export type StructureExample = { id: string; label: string; description: string; smiles: string; image?: string };

/** A small menu of starting structures; shares the page's popover menu behaviour. */
function ExampleMenu({
  examples,
  busy,
  disabled,
  onPick
}: {
  examples: readonly StructureExample[];
  busy: boolean;
  disabled: boolean;
  onPick: (smiles: string) => void;
}) {
  const menu = usePopoverMenu();

  return (
    <div ref={menu.rootRef} className="np-sw-menu">
      <button
        {...menu.triggerProps}
        type="button"
        className="np-sw-tool"
        data-workbench-tool="load"
        aria-label="加载示例"
        title="加载示例"
        disabled={disabled}
        aria-busy={busy || undefined}
      >
        {busy ? <LoaderCircle className="np-sw-spin" aria-hidden="true" /> : <Atom aria-hidden="true" />}
        <span>示例</span>
        <ChevronDown className="np-sw-menu__chevron" aria-hidden="true" />
      </button>
      {menu.open ? (
        <div {...menu.menuProps} className="np-sw-menu__list np-pop is-start" aria-label="示例结构">
          {examples.map((example) => (
            <button
              key={example.id}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="np-pop__item"
              onClick={() => {
                menu.close();
                onPick(example.smiles);
              }}
            >
              {example.image ? <img className="np-pop__thumb" src={example.image} alt="" /> : null}
              <span className="np-pop__text">
                <b>{example.label}</b>
                <small>{example.description}</small>
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Canvas writes from outside the editor (examples, history, buttons) must cancel pending SMILES
 * draft sync, stay within the workspace session and adopt the editor's SMILES afterwards.
 */
export async function runCanvasMutation(
  structure: StructureWorkspaceContext,
  canvas: StructureCanvasController,
  action: () => void | Promise<unknown>,
  writesEditor = true
) {
  const current = structure.workspace.guard();
  const finish = writesEditor ? structure.workspace.beginMutation() : () => {};
  try {
    await canvas.cancelSmilesDraftSync();
    if (!current()) return;
    await action();
  } finally {
    if (current()) canvas.adoptCanvasSmiles();
    finish();
  }
}

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
      aria-busy={busy || undefined}
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
  commandbarLabel,
  utilityActions = [],
  onLoadExample,
  onImportFile,
  onClear,
  onSync,
  onToggle3D,
  onSmilesDraftChange,
  emptyState,
  footer,
  clearLast = false,
  viewSwitch = "toggle",
  examples
}: StructureCanvasSurfaceProps) {
  const mutate = (action: () => void | Promise<unknown>, writesEditor = true) =>
    runCanvasMutation(structure, canvas, action, writesEditor);
  const showEmptyState = Boolean(emptyState) && canvas.isEditorReady && !canvas.isFlipped &&
    !structure.smiles.trim() && !canvas.smilesDraft.trim() && !canvas.isLoadingStructure;

  const smilesStatus = canvas.smilesDraftError || (
    canvas.smilesDraftState === "pending"
      ? "等待输入完成后自动同步…"
      : canvas.smilesDraftState === "syncing"
        ? "正在校验并同步到画板…"
        : canvas.feedback
  );

  const view3dDisabled = operationBusy || canvas.smilesDraftState !== "synced" || !canvas.isEditorReady;
  const clearTool = (
    <ToolButton
      label="清空画布"
      tool="clear"
      busy={canvas.isClearing}
      icon={<Eraser aria-hidden="true" />}
      disabled={operationBusy || !canvas.isEditorReady}
      danger
      onClick={() => void mutate(onClear)}
    />
  );

  return (
    <section className="np-sw-surface" aria-label="结构编辑工作区">
      <input
        ref={canvas.fileInputRef}
        className="np-sw-visually-hidden"
        type="file"
        accept="image/*"
        aria-label="导入结构图片"
        // Opened by the 导入图片 button; as a Tab stop it would hold invisible focus.
        tabIndex={-1}
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file && requestServiceAccess()) void mutate(() => onImportFile(file));
        }}
      />

      <header className="np-sw-commandbar" aria-label="结构工作台工具栏">
        {commandbarLabel ? <span className="np-sw-commandbar__label">{commandbarLabel}</span> : null}
        <div className="np-sw-toolbar">
          {examples?.length ? (
            <ExampleMenu
              examples={examples}
              busy={canvas.isLoadingStructure}
              disabled={operationBusy}
              onPick={(smiles) => { if (requestServiceAccess()) void mutate(() => canvas.loadStructure(smiles)); }}
            />
          ) : (
            <ToolButton
              label="加载示例"
              tool="load"
              busy={canvas.isLoadingStructure}
              icon={<Atom aria-hidden="true" />}
              disabled={operationBusy}
              onClick={() => void mutate(onLoadExample)}
            />
          )}
          <ToolButton
            label="导入图片"
            tool="import"
            busy={canvas.isImportingImage}
            icon={<ImagePlus aria-hidden="true" />}
            disabled={operationBusy}
            onClick={() => { if (requestServiceAccess()) canvas.fileInputRef.current?.click(); }}
          />
          {clearLast ? null : clearTool}
          <ToolButton
            label="生成SMILES"
            tool="sync"
            busy={canvas.isSyncing}
            icon={<RefreshCcw aria-hidden="true" />}
            disabled={operationBusy || !canvas.isEditorReady}
            primary
            onClick={() => void mutate(onSync, false)}
          />
          {viewSwitch === "segmented" ? (
            <div className="np-sw-seg" role="group" aria-label="画板视图">
              <button
                type="button"
                className={canvas.isFlipped ? undefined : "is-on"}
                aria-label="2D画布"
                aria-pressed={!canvas.isFlipped}
                disabled={canvas.isFlipped ? view3dDisabled : false}
                onClick={() => { if (canvas.isFlipped && requestServiceAccess()) void onToggle3D(); }}
              >
                2D
              </button>
              <button
                type="button"
                className={canvas.isFlipped ? "is-on" : undefined}
                data-workbench-tool="3d"
                aria-label="3D构象"
                aria-pressed={canvas.isFlipped}
                aria-busy={canvas.isFlipping || undefined}
                disabled={view3dDisabled}
                onClick={() => { if (!canvas.isFlipped && requestServiceAccess()) void onToggle3D(); }}
              >
                {canvas.isFlipping ? <LoaderCircle className="np-sw-spin" aria-hidden="true" /> : <Box aria-hidden="true" />}
                3D
              </button>
            </div>
          ) : (
            <ToolButton
              label={canvas.isFlipped ? "2D画布" : "3D构象"}
              tool="3d"
              busy={canvas.isFlipping}
              icon={<Box aria-hidden="true" />}
              disabled={view3dDisabled}
              active={canvas.isFlipped}
              onClick={() => { if (requestServiceAccess()) void onToggle3D(); }}
            />
          )}
          <CanvasLibraryActions workspace={structure.workspace} canvas={canvas} disabled={operationBusy} />
          {clearLast ? clearTool : null}
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
        <div ref={canvas.flipMotion?.ref} className={`np-sw-editor${canvas.isFlipped ? " is-flipped" : ""}`}>
          <div
            className={`np-sw-editor__layer np-sw-editor__layer--2d${canvas.isFlipped ? " is-hidden" : " is-visible"}`}
            aria-hidden={canvas.isFlipped}
            inert={canvas.isFlipped}
          >
            <StructureEditor workspace={structure.workspace} title={editorTitle}
              disabled={Boolean(structure.workspace.getSnapshot().documentBusy) || canvas.isLoadingStructure || canvas.isClearing || canvas.isImportingImage || canvas.smilesDraftState === "syncing"} />
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
        {showEmptyState && emptyState ? (
          <div className="np-sw-empty-state">
            {emptyState((smiles) => { if (requestServiceAccess()) void mutate(() => canvas.loadStructure(smiles)); })}
          </div>
        ) : null}
      </div>

      <footer className="np-sw-smiles" aria-labelledby="np-sw-smiles-label">
        <label id="np-sw-smiles-label">SMILES</label>
        <textarea
          rows={2}
          value={canvas.smilesDraft}
          maxLength={8000}
          disabled={Boolean(structure.workspace.getSnapshot().documentBusy)}
          spellCheck={false}
          aria-invalid={canvas.smilesDraftState === "error"}
          onChange={(event) => {
            const value = event.currentTarget.value;
            canvas.updateSmilesDraft(value);
            onSmilesDraftChange?.(value);
          }}
          placeholder="输入 SMILES 后将自动校验并同步到上方画板。"
          aria-label="SMILES 输入，自动同步到画板"
        />
        <button
          type="button"
          onClick={() => void canvas.copySmiles(canvas.smilesDraft)}
          disabled={!canvas.smilesDraft.trim()}
          aria-label="复制当前 SMILES 输入"
          title="复制当前 SMILES 输入"
        >
          {canvas.copyState === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
        </button>
        {smilesStatus ? (
          <p
            className={canvas.smilesDraftState === "error" ? "is-error" : ""}
            data-state={canvas.smilesDraftState}
            role={canvas.smilesDraftState === "error" ? "alert" : "status"}
            aria-live="polite"
          >
            {smilesStatus}
          </p>
        ) : null}
      </footer>
      {footer}
    </section>
  );
}
