import { useSyncExternalStore, type ReactNode } from "react";
import EditorImplementation, { engine } from "@structure-editor-engine";
import type { StructureWorkspace } from "../../structure/workspace";

/**
 * Business pages mount this surface, never an iframe ref or a window global.
 * `overlay` is page content drawn over the canvas. It shares the editor's isolated stacking
 * context, so it covers Ketcher's canvas but stays under Ketcher's own flyouts and menus.
 */
export function StructureEditor({ workspace, title, disabled = false, overlay }: {
  workspace: StructureWorkspace;
  title: string;
  disabled?: boolean;
  overlay?: ReactNode;
}) {
  const state = useSyncExternalStore(workspace.subscribe, workspace.getSnapshot);
  return (
    <div className="np-structure-editor" data-structure-editor data-editor-engine={engine}
      data-editor-status={state.status} aria-busy={state.status === "loading" || disabled}
      inert={state.status !== "error" && (disabled || state.status !== "ready")}>
      <EditorImplementation key={state.mountKey} workspace={workspace} title={title} />
      {overlay}
      {state.status === "loading" ? <div className="np-structure-editor__loading" role="status">画板加载中…</div> : null}
      {state.status === "error" ? (
        <div className="np-structure-editor__error" role="alert">
          <span>{state.notice || "结构编辑器加载失败。"}</span>
          <button type="button" onClick={workspace.retryEditor}>重试加载画板</button>
        </div>
      ) : null}
    </div>
  );
}
