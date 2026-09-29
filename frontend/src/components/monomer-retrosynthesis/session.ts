import { getSessionEpoch } from "../../auth/session";
import type { MonomerRetrosynthesisTargetRole } from "../../types";

export const MONOMER_RETROSYNTHESIS_DRAFT_KEY = "nexpoly:monomer-retrosynthesis:draft";
export const RETROSYNTHESIS_EXAMPLE = "C=C(C)C(=O)OC";
export const TARGET_ROLE_OPTIONS: { value: MonomerRetrosynthesisTargetRole; label: string }[] = [
  { value: "auto", label: "自动识别" },
  { value: "other", label: "通用单体" },
  { value: "diamine", label: "二胺" },
  { value: "dianhydride", label: "二酐" }
];

export type MonomerRetrosynthesisDraft = {
  smiles: string;
  targetRole: MonomerRetrosynthesisTargetRole;
  returnCount: string;
};
export type MonomerRetrosynthesisInput = { smiles?: string; notice?: string };

export function readRetrosynthesisDraft(): MonomerRetrosynthesisDraft {
  const empty: MonomerRetrosynthesisDraft = { smiles: "", targetRole: "auto", returnCount: "5" };
  try {
    const raw = window.sessionStorage.getItem(MONOMER_RETROSYNTHESIS_DRAFT_KEY);
    if (!raw) return empty;
    const value = JSON.parse(raw);
    if (value?.version !== 1 || typeof value.smiles !== "string" ||
      typeof value.returnCount !== "string" ||
      !TARGET_ROLE_OPTIONS.some(option => option.value === value.targetRole)) return empty;
    return { smiles: value.smiles, targetRole: value.targetRole, returnCount: value.returnCount };
  } catch { return empty; }
}

export function saveRetrosynthesisDraft(draft: MonomerRetrosynthesisDraft, identityEpoch: number) {
  if (identityEpoch !== getSessionEpoch()) return;
  try {
    window.sessionStorage.setItem(MONOMER_RETROSYNTHESIS_DRAFT_KEY, JSON.stringify({ version: 1, ...draft }));
  } catch { /* An unavailable cache must not interrupt editing. */ }
}
