import type { MonomerPolymerizationTargetClass } from "../../types";
import { DEFAULT_TARGET_CLASSES, clampInteger } from "./config";

export const MONOMER_POLYMERIZATION_DRAFT_KEY = "nexpoly:monomer-polymerization:draft";
export const MONOMER_POLYMERIZATION_DRAFT_VERSION = 1;

export type MonomerPolymerizationDraft = {
  monomerA: string;
  monomerB: string;
  targetClass: MonomerPolymerizationTargetClass;
  maxResults: number;
};

type StoredDraft = MonomerPolymerizationDraft & {
  version: typeof MONOMER_POLYMERIZATION_DRAFT_VERSION;
};

function storageAvailable() {
  return typeof window !== "undefined" && Boolean(window.sessionStorage);
}
export function readMonomerPolymerizationDraft(): MonomerPolymerizationDraft | null {
  if (!storageAvailable()) return null;
  try {
    const raw = window.sessionStorage.getItem(MONOMER_POLYMERIZATION_DRAFT_KEY);
    if (!raw) return null;
    const candidate = JSON.parse(raw) as Partial<StoredDraft>;
    if (
      candidate.version !== MONOMER_POLYMERIZATION_DRAFT_VERSION ||
      typeof candidate.monomerA !== "string" ||
      typeof candidate.monomerB !== "string" ||
      !DEFAULT_TARGET_CLASSES.includes(candidate.targetClass as MonomerPolymerizationTargetClass) ||
      typeof candidate.maxResults !== "number"
    ) {
      return null;
    }
    return {
      monomerA: candidate.monomerA,
      monomerB: candidate.monomerB,
      targetClass: candidate.targetClass as MonomerPolymerizationTargetClass,
      maxResults: clampInteger(candidate.maxResults, 1, 20)
    };
  } catch {
    return null;
  }
}

export function saveMonomerPolymerizationDraft(draft: MonomerPolymerizationDraft) {
  if (!storageAvailable()) return;
  try {
    const stored: StoredDraft = {
      version: MONOMER_POLYMERIZATION_DRAFT_VERSION,
      ...draft
    };
    window.sessionStorage.setItem(MONOMER_POLYMERIZATION_DRAFT_KEY, JSON.stringify(stored));
  } catch {
    // Draft persistence must never block the scientific workflow.
  }
}

export function clearMonomerPolymerizationDraft() {
  if (!storageAvailable()) return;
  try {
    window.sessionStorage.removeItem(MONOMER_POLYMERIZATION_DRAFT_KEY);
  } catch {
    // Ignore unavailable or quota-limited session storage.
  }
}
