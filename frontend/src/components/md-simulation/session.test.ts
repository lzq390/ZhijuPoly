// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from "vitest";
import type { MdDemoRunRequest } from "../../types";
import {
  clearMdSimulationDraft,
  MD_SIMULATION_DRAFT_KEY,
  readMdSimulationDraft,
  saveMdSimulationDraft
} from "./session";

const request: MdDemoRunRequest = {
  smiles: "*CC*",
  temperature: 300,
  pressure: 1,
  n_atom: 1000,
  n_chain: 10,
  forcefield: "GAFF2_mod"
};

beforeEach(() => window.sessionStorage.clear());

describe("MD simulation session draft", () => {
  it("round-trips all request fields and preserves temporarily blank numeric fields", () => {
    saveMdSimulationDraft({ ...request, pressure: Number.NaN });
    const raw = window.sessionStorage.getItem(MD_SIMULATION_DRAFT_KEY);
    expect(raw).toContain('"pressure":null');
    const restored = readMdSimulationDraft();
    expect(restored?.smiles).toBe("*CC*");
    expect(Number.isNaN(restored?.pressure)).toBe(true);
  });

  it("ignores incompatible drafts and supports explicit clearing", () => {
    window.sessionStorage.setItem(MD_SIMULATION_DRAFT_KEY, JSON.stringify({ ...request, version: 99 }));
    expect(readMdSimulationDraft()).toBeNull();
    saveMdSimulationDraft(request);
    clearMdSimulationDraft();
    expect(window.sessionStorage.getItem(MD_SIMULATION_DRAFT_KEY)).toBeNull();
  });
});
