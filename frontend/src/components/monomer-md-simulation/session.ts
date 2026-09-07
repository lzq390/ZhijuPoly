import type { MonomerMdFormalProtocol, MonomerMdRunMode } from "../../types";
import {
  FORMAL_PROTOCOLS,
  isRecord,
  validateFormalConfig
} from "./config";

export const MONOMER_MD_SESSION_KEY = "nexpoly:monomer-md-simulation:draft";
export const MONOMER_MD_SESSION_VERSION = 2;

export type MonomerMdSessionDraft = {
  runMode: MonomerMdRunMode;
  demoSmiles: string;
  selectedProtocol: MonomerMdFormalProtocol;
  configs: Partial<Record<MonomerMdFormalProtocol, Record<string, unknown>>>;
  templateFingerprints: Partial<Record<MonomerMdFormalProtocol, string>>;
};

export const EMPTY_MONOMER_MD_SESSION: MonomerMdSessionDraft = {
  runMode: "demo",
  demoSmiles: "",
  selectedProtocol: "Density",
  configs: {},
  templateFingerprints: {}
};

function isFormalProtocol(value: unknown): value is MonomerMdFormalProtocol {
  return typeof value === "string" && FORMAL_PROTOCOLS.includes(value as MonomerMdFormalProtocol);
}

export function parseMonomerMdSession(value: string | null): MonomerMdSessionDraft {
  if (!value) return EMPTY_MONOMER_MD_SESSION;
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!isRecord(parsed) || parsed.version !== MONOMER_MD_SESSION_VERSION) {
      return EMPTY_MONOMER_MD_SESSION;
    }
    const configs: MonomerMdSessionDraft["configs"] = {};
    if (isRecord(parsed.configs)) {
      for (const protocol of FORMAL_PROTOCOLS) {
        const candidate = parsed.configs[protocol];
        if (validateFormalConfig(candidate, protocol).valid && isRecord(candidate)) {
          configs[protocol] = candidate;
        }
      }
    }
    const templateFingerprints: MonomerMdSessionDraft["templateFingerprints"] = {};
    if (isRecord(parsed.templateFingerprints)) {
      for (const protocol of FORMAL_PROTOCOLS) {
        const value = parsed.templateFingerprints[protocol];
        if (typeof value === "string") templateFingerprints[protocol] = value;
      }
    }
    return {
      runMode: parsed.runMode === "formal" ? "formal" : "demo",
      demoSmiles: typeof parsed.demoSmiles === "string" ? parsed.demoSmiles : "",
      selectedProtocol: isFormalProtocol(parsed.selectedProtocol)
        ? parsed.selectedProtocol
        : "Density",
      configs,
      templateFingerprints
    };
  } catch {
    return EMPTY_MONOMER_MD_SESSION;
  }
}

export function loadMonomerMdSession(): MonomerMdSessionDraft {
  if (typeof window === "undefined") return EMPTY_MONOMER_MD_SESSION;
  try {
    return parseMonomerMdSession(window.sessionStorage.getItem(MONOMER_MD_SESSION_KEY));
  } catch {
    return EMPTY_MONOMER_MD_SESSION;
  }
}

export function saveMonomerMdSession(draft: MonomerMdSessionDraft): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(
      MONOMER_MD_SESSION_KEY,
      JSON.stringify({ version: MONOMER_MD_SESSION_VERSION, ...draft })
    );
  } catch {
    // A disabled or full sessionStorage must not make the workbench unusable.
  }
}
