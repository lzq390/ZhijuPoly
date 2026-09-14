import { useSyncExternalStore } from "react";
import EditorImplementation, { engine } from "@structure-editor-engine";
import type { StructureWorkspace } from "../../structure/workspace";

/** Business pages mount this surface, never an iframe ref or a window global. */
export function StructureEditor({ workspace, title, disabled = false }: { workspace: StructureWorkspace; title: string; disabled?: boolean }) {
  const state = useSyncExternalStore(workspace.subscribe, workspace.getSnapshot);
  return (
    <div className="np-structure-editor" data-structure-editor data-editor-engine={engine}
      data-editor-status={state.status} aria-busy={state.status === "loading" || disabled}
      inert={state.status !== "error" && (disabled || state.status !== "ready")}>
      <EditorImplementation key={state.mountKey} workspace={workspace} title={title} />
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
