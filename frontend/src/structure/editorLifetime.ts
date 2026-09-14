import type { StructureEditorHandle } from "./editor";

// An internal lifetime channel; business handles and Context stay engine agnostic.
// Retirement is synchronous, before React has a chance to unmount a stale owner.
const retirements = new WeakMap<StructureEditorHandle, () => void>();

export function registerEditorRetirement(editor: StructureEditorHandle, retire: () => void) {
  retirements.set(editor, retire);
}

export function retireEditor(editor: StructureEditorHandle | null) {
  if (!editor) return;
  const retire = retirements.get(editor);
  retirements.delete(editor);
  retire?.();
}
