import type { MdDemoRunRequest } from "../../types";

export const MD_SIMULATION_DRAFT_KEY = "nexpoly:md-simulation:draft";
export const MD_SIMULATION_DRAFT_VERSION = 1;

type StoredDraft = Omit<MdDemoRunRequest, "temperature" | "pressure" | "n_atom" | "n_chain"> & {
  version: typeof MD_SIMULATION_DRAFT_VERSION;
  temperature: number | null;
  pressure: number | null;
  n_atom: number | null;
  n_chain: number | null;
};

function storageAvailable() {
  if (typeof window === "undefined") return false;
  try {
    return Boolean(window.sessionStorage);
  } catch {
    return false;
  }
}

export function readMdSimulationDraft(): MdDemoRunRequest | null {
  if (!storageAvailable()) return null;
  try {
    const raw = window.sessionStorage.getItem(MD_SIMULATION_DRAFT_KEY);
    if (!raw) return null;
    const candidate = JSON.parse(raw) as Partial<StoredDraft>;
    if (
      candidate.version !== MD_SIMULATION_DRAFT_VERSION ||
      typeof candidate.smiles !== "string" ||
      !(candidate.temperature === null || (typeof candidate.temperature === "number" && Number.isFinite(candidate.temperature))) ||
      !(candidate.pressure === null || (typeof candidate.pressure === "number" && Number.isFinite(candidate.pressure))) ||
      !(candidate.n_atom === null || (typeof candidate.n_atom === "number" && Number.isFinite(candidate.n_atom))) ||
      !(candidate.n_chain === null || (typeof candidate.n_chain === "number" && Number.isFinite(candidate.n_chain))) ||
      typeof candidate.forcefield !== "string"
    ) {
      return null;
    }
    return {
      smiles: candidate.smiles,
      temperature: candidate.temperature ?? Number.NaN,
      pressure: candidate.pressure ?? Number.NaN,
      n_atom: candidate.n_atom ?? Number.NaN,
      n_chain: candidate.n_chain ?? Number.NaN,
      forcefield: candidate.forcefield
    };
  } catch {
    return null;
  }
}

export function saveMdSimulationDraft(draft: MdDemoRunRequest) {
  if (!storageAvailable()) return;
  try {
    const stored: StoredDraft = {
      version: MD_SIMULATION_DRAFT_VERSION,
      smiles: draft.smiles,
      temperature: Number.isFinite(draft.temperature) ? draft.temperature : null,
      pressure: Number.isFinite(draft.pressure) ? draft.pressure : null,
      n_atom: Number.isFinite(draft.n_atom) ? draft.n_atom : null,
      n_chain: Number.isFinite(draft.n_chain) ? draft.n_chain : null,
      forcefield: draft.forcefield
    };
    window.sessionStorage.setItem(MD_SIMULATION_DRAFT_KEY, JSON.stringify(stored));
  } catch {
    // Draft persistence must never block the scientific workflow.
  }
}

export function clearMdSimulationDraft() {
  if (!storageAvailable()) return;
  try {
    window.sessionStorage.removeItem(MD_SIMULATION_DRAFT_KEY);
  } catch {
    // Ignore unavailable or quota-limited session storage.
  }
}
