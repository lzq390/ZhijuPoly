import { vi } from "vitest";
import type { StructureWorkspaceContext } from "../types";
import { createKetcherAdapter, type KetcherInstance } from "../structure/editor";
import { StructureWorkspace } from "../structure/workspace";

/** Hook tests inject an editor capability; full mount/save contracts have their own tests. */
export function structureFixture(smiles: string, ketcher?: KetcherInstance): StructureWorkspaceContext {
  const workspace = new StructureWorkspace(smiles);
  vi.spyOn(workspace, "commitSmiles");
  if (ketcher) {
    const adapter = createKetcherAdapter(ketcher);
    adapter.settle = async () => {};
    vi.spyOn(workspace, "getEditor").mockImplementation(() => ({ ...adapter, isCurrent: workspace.guard() }));
    const snapshot = workspace.getSnapshot;
    let source = snapshot();
    let ready = { ...source, status: "ready" as const };
    vi.spyOn(workspace, "getSnapshot").mockImplementation(() => {
      if (source !== snapshot()) {
        source = snapshot();
        ready = { ...source, status: "ready" };
      }
      return ready;
    });
  }
  return { smiles, setSmiles: workspace.setSmiles, getCurrentSmiles: workspace.getCurrentSmiles, workspace };
}
