import { useState, useSyncExternalStore } from "react";
import { standardizeSmiles } from "../services/api";
import { StructureWorkspace } from "../structure/workspace";
import type { StructureWorkspaceContext } from "../types";

export function useStructureWorkspace(): StructureWorkspaceContext {
  const [workspace] = useState(() => new StructureWorkspace("", async (smiles) =>
    (await standardizeSmiles({ smiles })).standardized_smiles));
  const state = useSyncExternalStore(workspace.subscribe, workspace.getSnapshot);
  return {
    smiles: state.smiles,
    setSmiles: workspace.setSmiles,
    getCurrentSmiles: workspace.getCurrentSmiles,
    workspace
  };
}
