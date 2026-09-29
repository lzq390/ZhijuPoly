import { commitGuestDraft, readGuestDraft } from "../auth/guestDraft";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { assertSessionEpoch, getSessionEpoch } from "../auth/session";
import { standardizeSmiles } from "../services/api";
import { StructureWorkspace } from "../structure/workspace";
import type { StructureWorkspaceContext } from "../types";

export function useStructureWorkspace({ guest = false, initialSmiles }: { guest?: boolean; initialSmiles?: string } = {}): StructureWorkspaceContext {
  const identityEpoch = useRef(getSessionEpoch()).current;
  const [{ workspace, importedDraft }] = useState(() => {
    const importedDraft = initialSmiles === undefined ? readGuestDraft() : null;
    const workspace = new StructureWorkspace(initialSmiles ?? importedDraft?.smiles ?? "", async (smiles) => {
      assertSessionEpoch(identityEpoch);
      if (guest) return smiles;
      const result = await standardizeSmiles({ smiles });
      assertSessionEpoch(identityEpoch);
      return result.standardized_smiles;
    });
    return { workspace, importedDraft };
  });
  useEffect(() => { commitGuestDraft(importedDraft); }, [importedDraft]);
  const state = useSyncExternalStore(workspace.subscribe, workspace.getSnapshot);
  return {
    smiles: state.smiles,
    setSmiles: workspace.setSmiles,
    getCurrentSmiles: async () => {
      if (identityEpoch !== getSessionEpoch()) return "";
      const smiles = await workspace.getCurrentSmiles();
      // Callers may submit immediately after local editor work finishes.
      return identityEpoch === getSessionEpoch() ? smiles : "";
    },
    workspace
  };
}
