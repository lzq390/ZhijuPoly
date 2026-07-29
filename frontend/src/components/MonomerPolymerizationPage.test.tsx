// @vitest-environment jsdom

import { createRef } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MonomerPolymerizationStatusResponse, StructureWorkspaceContext } from "../types";
import { MonomerPolymerizationPage, SMIPOLY_POLYIMIDE_FIXTURE } from "./MonomerPolymerizationPage";

const apiMocks = vi.hoisted(() => ({
  fetchStatus: vi.fn(),
  runPolymerization: vi.fn()
}));

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    fetchMonomerPolymerizationStatus: apiMocks.fetchStatus,
    runMonomerPolymerization: apiMocks.runPolymerization
  };
});

const readyStatus: MonomerPolymerizationStatusResponse = {
  enabled: true,
  available: true,
  default_target_class: "polyimide",
  available_target_classes: ["polyimide"],
  target_requirements: {
    polyimide: {
      min_monomers: 2,
      max_monomers: 2,
      monomer_b_required: true,
      note: "Polyimide requires a diamine and a dianhydride."
    }
  },
  max_results_limit: 20,
  message: "ready"
};

function makeStructure(): StructureWorkspaceContext {
  return {
    smiles: SMIPOLY_POLYIMIDE_FIXTURE.monomerA,
    setSmiles: vi.fn(),
    iframeRef: createRef<HTMLIFrameElement>(),
    setIsReady: vi.fn(),
    getCurrentSmiles: vi.fn().mockResolvedValue(SMIPOLY_POLYIMIDE_FIXTURE.monomerA)
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MonomerPolymerizationPage polyimide inputs", () => {
  it("uses the pinned-runtime dianhydride fixture and blocks submit while required monomer B is empty", async () => {
    apiMocks.fetchStatus.mockResolvedValue(readyStatus);
    apiMocks.runPolymerization.mockResolvedValue({
      input_monomers: [],
      target_class: "polyimide",
      query_time_ms: 1,
      total: 0,
      results: [],
      warnings: []
    });

    render(
      <MonomerPolymerizationPage
        structure={makeStructure()}
        onEditStructure={vi.fn()}
        onBackHome={vi.fn()}
      />
    );
    await waitFor(() => expect(apiMocks.fetchStatus).toHaveBeenCalledOnce());

    const monomerB = screen.getByPlaceholderText(`示例：${SMIPOLY_POLYIMIDE_FIXTURE.monomerB}`);
    const submit = screen.getByRole("button", { name: "运行聚合" }) as HTMLButtonElement;
    expect(SMIPOLY_POLYIMIDE_FIXTURE.monomerB).toBe("O=C1OC(=O)c2cc3c(cc21)C(=O)OC3=O");
    expect(submit.disabled).toBe(true);

    fireEvent.change(monomerB, { target: { value: SMIPOLY_POLYIMIDE_FIXTURE.monomerB } });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => {
      expect(apiMocks.runPolymerization).toHaveBeenCalledWith({
        monomer_a_smiles: SMIPOLY_POLYIMIDE_FIXTURE.monomerA,
        monomer_b_smiles: SMIPOLY_POLYIMIDE_FIXTURE.monomerB,
        target_class: "polyimide",
        max_results: 10
      });
    });
  });
});
