// @vitest-environment jsdom

import { createRef } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MonomerPolymerizationResponse,
  MonomerPolymerizationStatusResponse,
  StructureWorkspaceContext
} from "../types";
import {
  MONOMER_POLYMERIZATION_DRAFT_KEY
} from "./monomer-polymerization/session";
import { MonomerPolymerizationPage, SMIPOLY_POLYIMIDE_FIXTURE } from "./MonomerPolymerizationPage";

const apiMocks = vi.hoisted(() => ({
  fetchStatus: vi.fn(),
  runPolymerization: vi.fn(),
  fetchStructure2D: vi.fn()
}));

vi.mock("../services/api", () => ({
  fetchMonomerPolymerizationStatus: apiMocks.fetchStatus,
  runMonomerPolymerization: apiMocks.runPolymerization,
  fetchStructure2D: apiMocks.fetchStructure2D
}));

const readyStatus: MonomerPolymerizationStatusResponse = {
  enabled: true,
  available: true,
  default_target_class: "polyimide",
  available_target_classes: ["polyimide", "polyether"],
  target_requirements: {
    polyimide: {
      min_monomers: 2,
      max_monomers: 2,
      monomer_b_required: true,
      note: "Requires a diamine and a dianhydride monomer."
    },
    polyether: {
      min_monomers: 1,
      max_monomers: 2,
      monomer_b_required: false,
      note: "Allows a single submitted monomer when SMiPoly has a matching rule."
    }
  },
  max_results_limit: 20,
  message: "ready"
};

const response: MonomerPolymerizationResponse = {
  input_monomers: [
    {
      role: "monomer_a",
      input_smiles: SMIPOLY_POLYIMIDE_FIXTURE.monomerA,
      canonical_smiles: "Nc1ccc(N)cc1"
    },
    {
      role: "monomer_b",
      input_smiles: SMIPOLY_POLYIMIDE_FIXTURE.monomerB,
      canonical_smiles: "O=C1OC(=O)c2cc3c(cc21)C(=O)OC3=O"
    }
  ],
  target_class: "polyimide",
  query_time_ms: 12.4,
  total: 4,
  results: [
    {
      rank: 1,
      monomer_a_smiles: SMIPOLY_POLYIMIDE_FIXTURE.monomerA,
      monomer_b_smiles: SMIPOLY_POLYIMIDE_FIXTURE.monomerB,
      polymer_smiles: "*N(c1ccc(N*)cc1)C(=O)c1ccc(C(=O))cc1*",
      polymer_class: "polyimide",
      reaction_id: 17,
      reaction_name: "Polyimide formation",
      reactset: [SMIPOLY_POLYIMIDE_FIXTURE.monomerA, SMIPOLY_POLYIMIDE_FIXTURE.monomerB],
      structure_svg: [
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 20 20\">",
        "<rect x=\"0\" y=\"0\" width=\"20\" height=\"20\" fill=\"#fff\" />",
        "<path d=\"M1 10h18\" />",
        "</svg>"
      ].join("")
    }
  ],
  warnings: [
    "Filtered SMiPoly rows that involved automatically added auxiliary molecules outside the submitted monomers.",
    "custom upstream notice"
  ]
};

function makeStructure(smiles: string = SMIPOLY_POLYIMIDE_FIXTURE.monomerA): StructureWorkspaceContext {
  return {
    smiles,
    setSmiles: vi.fn(),
    iframeRef: createRef<HTMLIFrameElement>(),
    setIsReady: vi.fn(),
    getCurrentSmiles: vi.fn().mockResolvedValue(smiles)
  };
}

function renderPage(structure = makeStructure(), onEditStructure = vi.fn()) {
  return {
    ...render(
      <MonomerPolymerizationPage
        structure={structure}
        onEditStructure={onEditStructure}
      />
    ),
    structure,
    onEditStructure
  };
}

beforeEach(() => {
  window.sessionStorage.clear();
  apiMocks.fetchStatus.mockReset().mockResolvedValue(readyStatus);
  apiMocks.runPolymerization.mockReset().mockResolvedValue(response);
  apiMocks.fetchStructure2D.mockReset().mockResolvedValue({
    structure_svg: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 10 10\"><circle cx=\"5\" cy=\"5\" r=\"4\" /></svg>"
  });
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) }
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MonomerPolymerizationPage", () => {
  it("使用统一强调工作面样式", () => {
    const view = renderPage();
    const workSurface = view.container.querySelector(".np-mp-surface");
    const moduleToolbar = view.container.querySelector(".np-mp-module-toolbar");
    expect(workSurface?.classList.contains("np-sw-accented-surface")).toBe(true);
    expect(moduleToolbar).not.toBeNull();
    expect(moduleToolbar?.querySelector('[role="status"]')).not.toBeNull();
    expect(workSurface?.querySelector('[role="status"]')).toBeNull();
    expect(workSurface?.querySelector(".np-mp-surface-mark svg")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "正向聚合设置" })).toBeTruthy();
    expect(screen.queryByText("SMIPOLY FORWARD POLYMERIZATION")).toBeNull();
  });

  it("uses remote target requirements, delays the missing-B message, and submits the unchanged contract", async () => {
    renderPage();
    const serviceStatus = await screen.findByRole("status");
    expect(serviceStatus.textContent).toBe("准备就绪");
    expect(serviceStatus.textContent).not.toContain("SMiPoly");
    expect(serviceStatus.querySelector(".np-mp-ready-dot")).not.toBeNull();
    expect(serviceStatus.querySelector("svg")).toBeNull();

    const monomerB = screen.getByLabelText("SMILES", { selector: "#monomer-b-smiles" });
    const submit = screen.getByRole("button", { name: "聚合" }) as HTMLButtonElement;
    expect(screen.queryByText("Polyimide 需要单体 B。")).toBeNull();
    expect(submit.disabled).toBe(true);

    fireEvent.blur(monomerB);
    expect(screen.getByText("Polyimide 需要单体 B。")).toBeTruthy();
    fireEvent.change(monomerB, { target: { value: SMIPOLY_POLYIMIDE_FIXTURE.monomerB } });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => {
      expect(apiMocks.runPolymerization).toHaveBeenCalledWith(
        {
          monomer_a_smiles: SMIPOLY_POLYIMIDE_FIXTURE.monomerA,
          monomer_b_smiles: SMIPOLY_POLYIMIDE_FIXTURE.monomerB,
          target_class: "polyimide",
          max_results: 10
        },
        expect.any(AbortSignal)
      );
    });
    const drawer = await screen.findByRole("dialog", { name: "正向聚合结果" });
    expect(within(drawer).getByText("1 / 4")).toBeTruthy();
    expect(within(drawer).getByText("已忽略需要额外辅助分子的结果，因为这些分子不在本次输入中。")).toBeTruthy();
    expect(within(drawer).getByText(/SMiPoly 信息：/)).toBeTruthy();
    expect(within(drawer).getByRole("heading", { name: "Polyimide", level: 3 })).toBeTruthy();
    expect(within(drawer).getByText("反应 ID 17")).toBeTruthy();
    expect(within(drawer).getByText("聚合物 SMILES")).toBeTruthy();
    expect(within(drawer).getByText("单体组合")).toBeTruthy();
    expect(within(drawer).getByText("反应名称")).toBeTruthy();
    const candidatePreview = within(drawer).getByAltText("候选 1 的聚合物结构") as HTMLImageElement;
    const candidateSvg = decodeURIComponent(candidatePreview.src.slice(candidatePreview.src.indexOf(",") + 1));
    expect(candidateSvg).not.toContain("<rect");
    expect(candidateSvg).toContain("<path");
  });

  it("fills and previews the recommended example when Enter is pressed in an empty monomer slot", async () => {
    renderPage(makeStructure(""));
    await screen.findByText("准备就绪");
    const monomerA = screen.getByLabelText("SMILES", { selector: "#monomer-a-smiles" }) as HTMLTextAreaElement;
    const monomerB = screen.getByLabelText("SMILES", { selector: "#monomer-b-smiles" }) as HTMLTextAreaElement;

    expect(monomerA.placeholder).toBe(`推荐示例：${SMIPOLY_POLYIMIDE_FIXTURE.monomerA}`);
    expect(monomerB.placeholder).toBe(`推荐示例：${SMIPOLY_POLYIMIDE_FIXTURE.monomerB}`);
    expect(screen.getAllByText("请先输入 SMILES")).toHaveLength(2);
    expect(screen.queryByText(/失焦/)).toBeNull();

    expect(fireEvent.keyDown(monomerA, { key: "Enter" })).toBe(false);
    expect(monomerA.value).toBe(SMIPOLY_POLYIMIDE_FIXTURE.monomerA);
    await waitFor(() => {
      expect(apiMocks.fetchStructure2D).toHaveBeenCalledWith(
        SMIPOLY_POLYIMIDE_FIXTURE.monomerA,
        expect.any(AbortSignal)
      );
    });

    expect(fireEvent.keyDown(monomerB, { key: "Enter" })).toBe(false);
    expect(monomerB.value).toBe(SMIPOLY_POLYIMIDE_FIXTURE.monomerB);
    await waitFor(() => {
      expect(apiMocks.fetchStructure2D).toHaveBeenCalledWith(
        SMIPOLY_POLYIMIDE_FIXTURE.monomerB,
        expect.any(AbortSignal)
      );
    });

    fireEvent.change(monomerA, { target: { value: "CCN" } });
    expect(fireEvent.keyDown(monomerA, { key: "Enter" })).toBe(true);
    expect(monomerA.value).toBe("CCN");
  });

  it("keeps monomer B when switching to an optional target and rejects dummy atoms locally", async () => {
    renderPage();
    await screen.findByText("准备就绪");
    const monomerA = screen.getByLabelText("SMILES", { selector: "#monomer-a-smiles" });
    const monomerB = screen.getByLabelText("SMILES", { selector: "#monomer-b-smiles" });
    const target = screen.getByRole("combobox", { name: /POLYMER CLASS/ });

    fireEvent.change(monomerB, { target: { value: "CCO" } });
    fireEvent.click(target);
    const selectedTarget = screen.getByRole("option", { name: /Polyimide/ });
    expect(selectedTarget.getAttribute("aria-selected")).toBe("true");
    expect(selectedTarget.querySelector("svg")).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: /Polyether/ }));
    expect((monomerB as HTMLTextAreaElement).value).toBe("CCO");
    expect(screen.getByText("可选")).toBeTruthy();

    fireEvent.change(monomerA, { target: { value: "*CC*" } });
    expect(screen.getByText("普通单体 SMILES 不应包含 * 连接点。")).toBeTruthy();
    expect((screen.getByRole("button", { name: "聚合" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("imports the shared structure independently into both slots and saves before opening the editor", async () => {
    const structure = makeStructure("CCO");
    const onEditStructure = vi.fn();
    renderPage(structure, onEditStructure);
    await screen.findByText("准备就绪");
    const importButtons = screen.getAllByRole("button", { name: "导入共享结构" });

    fireEvent.click(importButtons[1]);
    await waitFor(() => expect((screen.getByLabelText("SMILES", { selector: "#monomer-b-smiles" }) as HTMLTextAreaElement).value).toBe("CCO"));
    expect(structure.getCurrentSmiles).toHaveBeenCalled();
    await waitFor(() => expect(apiMocks.fetchStructure2D).toHaveBeenCalledWith("CCO", expect.any(AbortSignal)));

    fireEvent.click(screen.getByRole("button", { name: "编辑共享结构" }));
    expect(onEditStructure).toHaveBeenCalledOnce();
    expect(window.sessionStorage.getItem(MONOMER_POLYMERIZATION_DRAFT_KEY)).toContain('"monomerB":"CCO"');
  });

  it("marks prior results stale, clears only results, and removes the draft on reset", async () => {
    renderPage();
    await screen.findByText("准备就绪");
    const monomerA = screen.getByLabelText("SMILES", { selector: "#monomer-a-smiles" });
    const monomerB = screen.getByLabelText("SMILES", { selector: "#monomer-b-smiles" });
    fireEvent.change(monomerB, { target: { value: SMIPOLY_POLYIMIDE_FIXTURE.monomerB } });
    fireEvent.click(screen.getByRole("button", { name: "聚合" }));
    await screen.findByText("反应 ID 17");

    fireEvent.change(monomerA, { target: { value: "CCN" } });
    expect(screen.getByText("这些结果基于上一次运行；当前输入已更改。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "清空结果" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "正向聚合结果" })).toBeNull());
    expect((monomerA as HTMLTextAreaElement).value).toBe("CCN");

    await waitFor(() => expect(window.sessionStorage.getItem(MONOMER_POLYMERIZATION_DRAFT_KEY)).not.toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "重置" }));
    expect(window.sessionStorage.getItem(MONOMER_POLYMERIZATION_DRAFT_KEY)).toBeNull();
    expect((screen.getByLabelText("SMILES", { selector: "#monomer-a-smiles" }) as HTMLTextAreaElement).value)
      .toBe(SMIPOLY_POLYIMIDE_FIXTURE.monomerA);
    expect((screen.getByLabelText("SMILES", { selector: "#monomer-b-smiles" }) as HTMLTextAreaElement).value)
      .toBe("");
  });

  it("removes the white SVG canvas so monomer previews inherit the workbench background", async () => {
    apiMocks.fetchStructure2D.mockResolvedValue({
      structure_svg: [
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 10 10\">",
        "<rect x=\"0\" y=\"0\" width=\"10\" height=\"10\" fill=\"#fff\" />",
        "<path d=\"M1 5h8\" />",
        "</svg>"
      ].join("")
    });

    renderPage();
    const preview = await screen.findByAltText("单体 A 的 2D 结构预览") as HTMLImageElement;
    const renderedSvg = decodeURIComponent(preview.src.slice(preview.src.indexOf(",") + 1));
    expect(renderedSvg).not.toContain("<rect");
    expect(renderedSvg).toContain("<path");
  });
});
