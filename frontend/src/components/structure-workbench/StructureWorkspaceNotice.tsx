import { useSyncExternalStore } from "react";
import type { StructureWorkspace } from "../../structure/workspace";

export function StructureWorkspaceNotice({ workspace }: { workspace: StructureWorkspace }) {
  const { notice } = useSyncExternalStore(workspace.subscribe, workspace.getSnapshot);
  if (!notice) return null;
  return (
    <div className="np-structure-notice" role="status">
      <span>{notice}</span>
      <button type="button" onClick={workspace.dismissNotice} aria-label="关闭画板同步提示">关闭</button>
    </div>
  );
}
