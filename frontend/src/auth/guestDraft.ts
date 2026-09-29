import { getSessionEpoch, onSessionRetired } from "./session";
// One explicit, in-memory handoff from the guest canvas; never read from storage.
type GuestDraft = Readonly<{ smiles: string; identityEpoch: number }>;
let importedGuestDraft: GuestDraft | null = null;
export function acceptGuestDraft(smiles: string) {
  importedGuestDraft = { smiles, identityEpoch: getSessionEpoch() };
}
// Render may be retried or abandoned. Reading must not consume the handoff.
export function readGuestDraft() {
  return importedGuestDraft?.identityEpoch === getSessionEpoch() ? importedGuestDraft : null;
}
export function commitGuestDraft(draft: GuestDraft | null) {
  // An older mount must never acknowledge a newer import or another identity.
  if (draft && draft === importedGuestDraft && draft.identityEpoch === getSessionEpoch()) {
    importedGuestDraft = null;
  }
}

onSessionRetired(() => { importedGuestDraft = null; });
