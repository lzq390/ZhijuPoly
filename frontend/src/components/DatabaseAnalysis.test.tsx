/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  browseDftEnergySteps,
  browseDftMolecules,
  browseExperimentalProcessRecords,
  browseExperimentalPropertyRecords,
  browseFormulationRecords,
  browseStructurePropertyRecords,
  fetchDatabaseAnalytics,
  fetchDatabaseDatasetSummary,
  fetchDftMolecule,
  fetchDftPcaSample
} from "../services/api";
import { DatabaseAnalysis } from "./DatabaseAnalysis";
import type {
  DatabaseAnalyticsResponse,
  DftEnergyStepRecord,
  DftMoleculeBrowserRecord,
  ExperimentalProcessRecord,
  ExperimentalPropertyRecord,
  FormulationRecord,
  StructurePropertyRecord
} from "../types";

vi.mock("../services/api", () => ({
  browseDftEnergySteps: vi.fn(),
  browseDftMolecules: vi.fn(),
  browseExperimentalProcessRecords: vi.fn(),
  browseExperimentalPropertyRecords: vi.fn(),
  browseFormulationRecords: vi.fn(),
  browseStructurePropertyRecords: vi.fn(),
  fetchDatabaseAnalytics: vi.fn(),
  fetchDatabaseDatasetSummary: vi.fn(),
  fetchDftMolecule: vi.fn(),
  fetchDftPcaSample: vi.fn()
}));

const mockSummary = vi.mocked(fetchDatabaseDatasetSummary);
const mockAnalytics = vi.mocked(fetchDatabaseAnalytics);
const mockBrowseProcess = vi.mocked(browseExperimentalProcessRecords);
const mockBrowseProperty = vi.mocked(browseExperimentalPropertyRecords);
const mockBrowseStructure = vi.mocked(browseStructurePropertyRecords);
const mockBrowseFormulation = vi.mocked(browseFormulationRecords);
const mockBrowseDft = vi.mocked(browseDftMolecules);
const mockBrowseDftSteps = vi.mocked(browseDftEnergySteps);
const mockPca = vi.mocked(fetchDftPcaSample);
const mockMolecule = vi.mocked(fetchDftMolecule);

const summaryResponse = {
  query_time_ms: 2,
  backend: "postgres",
  datasets: [
    ["process", "Experimental Process Data", 100],
    ["property", "Experimental Property Data", 80],
    ["structureEffect", "Polymer Structure-Property Data", 60],
    ["dft", "DFT Conformation Data", 40],
    ["formulation", "Formulation Ratio Data", 20]
  ].map(([key, title, total]) => ({
    key: String(key),
    title: String(title),
    total_records: Number(total),
    data_source: "postgres",
    source_status: "ready",
    source_message: null,
    latest_import_status: "completed",
    latest_import_finished_at: "2026-08-10T10:30:00+08:00"
  }))
};

const range = { label: "Tg", count: 30, min: -20, p5: 0, median: 120, p95: 280, max: 350 };
const analyticsDatasets = {
  process: {
    rows: 100,
    uniqueRecordIds: 70,
    uniquePolymers: 45,
    uniqueProducts: 32,
    avgProcessTextLength: 420.5,
    processSignalSummary: { extractedRows: 40, uniqueSnippets: 100, medianChars: 310 },
    processSignals: [{ label: "temperature", value: 60, total: 100 }],
    topTerms: [{ label: "polymerization", value: 10 }],
    topProducts: [{ label: "Polyimide", value: 8 }],
    topMaterials: [{ label: "NMP", value: 6 }]
  },
  property: {
    rows: 80,
    uniquePolymers: 42,
    uniqueProperties: 18,
    categories: [{ label: "thermal", value: 50, color: "#3b82f6" }],
    topProperties: [{ label: "glass transition temperature", value: 30 }],
    ranges: [range],
    categoryTop: [{ label: "thermal: glass transition temperature", value: 30 }]
  },
  structureEffect: {
    rows: 60,
    uniqueSmiles: 38,
    properties: [{ label: "Tg K", value: 24 }],
    units: [{ label: "K", value: 24, color: "#8b5cf6" }],
    sources: [{ label: "experimental", value: 40 }],
    sourceMatrix: [{ label: "Tg K", exp: 20, sim: 4, na: 0 }],
    ranges: [range]
  },
  dft: {
    rows: 40,
    molCount: 12,
    energyRange: range,
    gapRange: { ...range, median: 3.2 },
    orbitalDistributions: [],
    stepRange: { ...range, median: 24, max: 41 },
    atomRange: { ...range, median: 18 },
    atomTotals: [{ label: "C", value: 80 }],
    convergence: [{ label: "converged", value: 10 }, { label: "false", value: 2 }]
  },
  formulation: {
    files: 4,
    rows: 20,
    coverage: [{ label: "Formula and dosage", count: 18, pct: 90 }],
    componentCounts: [{ label: "3", value: 12 }, { label: "4", value: 8 }],
    topComponents: [{ label: "DGEBA", value: 8 }],
    polymerFamilies: [{ label: "epoxy", value: 12, color: "#0f9f8f" }],
    ratioTypes: [{ label: "ratio colon", value: 10 }],
    tempBands: [{ label: "120-179 C", value: 9 }],
    timeUnits: [{ label: "hours", value: 11 }],
    topCatalysts: [{ label: "DABCO", value: 4 }],
    topSolvents: [{ label: "DMF", value: 5 }],
    examples: [{ title: "EP-1", polymer: "epoxy", formula: "A:B = 1:1", condition: "180 C · 3 h" }]
  }
};

const analyticsResponse: DatabaseAnalyticsResponse = {
  query_time_ms: 3,
  backend: "postgres",
  source: "snapshot",
  generated_at: "2026-08-10T11:24:00+08:00",
  datasets: analyticsDatasets
};

function emptyBrowse<T = never>(results: T[] = []) {
  return {
    query: "",
    page: 1,
    page_size: 10,
    query_time_ms: 1,
    total_records: results.length,
    matched_records: results.length,
    data_source: "postgres",
    source_status: "ready",
    source_message: null,
    results
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockSummary.mockResolvedValue(summaryResponse);
  mockAnalytics.mockResolvedValue(analyticsResponse);
  mockBrowseProcess.mockResolvedValue(emptyBrowse<ExperimentalProcessRecord>([
    {
      source_file: "process.csv",
      source_row_number: 1,
      polymer_id: "PI-1",
      polymer_name: "polyimide",
      product_name: "PI film",
      process_flow_original_text: "polymerization at 180 C",
      material_original_text: "NMP and PMDA"
    }
  ]));
  mockBrowseProperty.mockResolvedValue(emptyBrowse<ExperimentalPropertyRecord>());
  mockBrowseStructure.mockResolvedValue(emptyBrowse<StructurePropertyRecord>());
  mockBrowseFormulation.mockResolvedValue(emptyBrowse<FormulationRecord>());
  mockBrowseDft.mockResolvedValue({
    ...emptyBrowse<DftMoleculeBrowserRecord>(),
    total_step_records: 0,
    average_steps: 0,
    max_steps: 0
  });
  mockBrowseDftSteps.mockResolvedValue(emptyBrowse<DftEnergyStepRecord>());
  mockPca.mockResolvedValue({
    query_time_ms: 1,
    total: 2,
    results: [
      { mol_id: "DFT-1", x: 0, y: 0, z: 0, n_atoms: 3, final_step: 2, homo_ev: -5, lumo_ev: -2, gap_ev: 3, dipole_moment: 1 },
      { mol_id: "DFT-2", x: 1, y: 1, z: 1, n_atoms: 4, final_step: 3, homo_ev: -4, lumo_ev: -1, gap_ev: 3, dipole_moment: 2 }
    ]
  });
  mockMolecule.mockResolvedValue({
    mol_id: "DFT-1",
    range_group: "small",
    final_step: 2,
    n_atoms: 3,
    coordinates: [[6, 0, 0, 0], [8, 1.2, 0, 0], [1, -1, 0, 0]],
    scf_energy: -100,
    zero_point_energy: 0,
    thermal_enthalpy: 0,
    gibbs_free_energy: -99,
    lowest_freq: 10,
    dipole_moment: 1,
    homo_ev: -5,
    lumo_ev: -2,
    gap_ev: 3,
    is_converged: "converged",
    trace: [{ step: 0, scf_energy: -99, homo_ev: -5, lumo_ev: -2, gap_ev: 3 }, { step: 2, scf_energy: -100, homo_ev: -5, lumo_ev: -2, gap_ev: 3 }]
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  document.getElementById("3dmol-script")?.remove();
});

function renderAnalysis(selectedKey: "process" | "property" | "structureEffect" | "dft" | "formulation" | null = null) {
  const onOpenDataset = vi.fn();
  const onBackDatabase = vi.fn();
  const view = render(
    <DatabaseAnalysis
      selectedKey={selectedKey}
      onBackHome={vi.fn()}
      onBackDatabase={onBackDatabase}
      onOpenDataset={onOpenDataset}
    />
  );
  return { ...view, onOpenDataset, onBackDatabase };
}

describe("数据库分析工作台", () => {
  it("以紧凑全库概览替代 Hero，并通过数据集浮层切换深链", async () => {
    const view = renderAnalysis();

    expect(await screen.findByText("数据集概览")).not.toBeNull();
    expect(screen.getByText("全库概览")).not.toBeNull();
    expect(screen.getByText("统计方式")).not.toBeNull();
    expect(screen.getByText("预计算统计")).not.toBeNull();
    expect(screen.queryByText("Polymer Data Platform")).toBeNull();
    const toolbar = view.container.querySelector(".dba-toolbar");
    expect(toolbar).not.toBeNull();
    expect(within(toolbar as HTMLElement).getAllByRole("button", { hidden: false })).toHaveLength(2);

    const trigger = screen.getByRole("button", { name: "数据集" });
    fireEvent.click(trigger);
    const popover = await screen.findByRole("region", { name: "选择数据集" });
    fireEvent.click(within(popover).getByRole("button", { name: /实验过程数据/ }));
    expect(view.onOpenDataset).toHaveBeenCalledWith("process");

    fireEvent.click(trigger);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "选择数据集" })).toBeNull();
    expect(document.activeElement).toBe(trigger);

    fireEvent.click(trigger);
    expect(await screen.findByRole("region", { name: "选择数据集" })).not.toBeNull();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("region", { name: "选择数据集" })).toBeNull();
  });

  it.each([
    ["property" as const, "性能类别"],
    ["structureEffect" as const, "来源 × 属性矩阵"],
    ["formulation" as const, "字段覆盖率"]
  ])("使用真实 analytics payload 渲染 %s 工作面", async (dataset, panelTitle) => {
    const view = renderAnalysis(dataset);

    expect(await screen.findByText(panelTitle)).not.toBeNull();
    expect(screen.queryByText("Polymer Data Platform")).toBeNull();
    view.unmount();
  });

  it("数据源未就绪时显示面板级保留态", async () => {
    mockSummary.mockResolvedValue({
      ...summaryResponse,
      datasets: summaryResponse.datasets.map((dataset) =>
        dataset.key === "process"
          ? { ...dataset, source_status: "reserved", source_message: "source is being prepared" }
          : dataset
      )
    });

    renderAnalysis("process");

    expect(await screen.findByText("该数据源尚未就绪")).not.toBeNull();
    expect(screen.queryByText("过程关键词")).toBeNull();
  });

  it("刷新期间保留旧统计并在成功后显示局部完成提示", async () => {
    let resolveRefresh: ((value: DatabaseAnalyticsResponse) => void) | undefined;
    mockAnalytics
      .mockResolvedValueOnce(analyticsResponse)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRefresh = resolve; }));
    renderAnalysis("process");
    expect(await screen.findByText("过程关键词")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "刷新数据" }));
    const refreshButton = screen.getByRole("button", { name: "刷新中" });
    expect(refreshButton.getAttribute("aria-busy")).toBe("true");
    expect(refreshButton.classList.contains("is-refreshing")).toBe(true);
    expect(await screen.findByText(/正在按当前数据库实时重算/)).not.toBeNull();
    expect(screen.getByText("过程关键词")).not.toBeNull();

    resolveRefresh?.({ ...analyticsResponse, source: "live", generated_at: null });
    expect(await screen.findByText(/已按当前数据库完成实时重算/)).not.toBeNull();
    expect(screen.getByText("实时重算")).not.toBeNull();
  });

  it("刷新失败时保留旧内容、使用中文错误并支持重试", async () => {
    mockAnalytics
      .mockResolvedValueOnce(analyticsResponse)
      .mockRejectedValueOnce(new Error("network failed"))
      .mockResolvedValueOnce({ ...analyticsResponse, source: "live" });
    renderAnalysis("property");
    expect(await screen.findByText("性能类别")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "刷新数据" }));
    expect(await screen.findByText(/统计更新失败，仍显示上次成功结果：分析数据加载失败/)).not.toBeNull();
    expect(screen.getByText("性能类别")).not.toBeNull();
    expect(screen.queryByText("network failed")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText(/已按当前数据库完成实时重算/)).not.toBeNull();
  });

  it("图表下钻打开真实记录抽屉，并支持关闭、重开与键盘调宽", async () => {
    renderAnalysis("process");
    const chartButton = await screen.findByRole("button", { name: /polymerization/ });
    fireEvent.click(chartButton);

    expect(await screen.findByRole("dialog", { name: "原始记录" })).not.toBeNull();
    expect(await screen.findByText("PI film")).not.toBeNull();
    await waitFor(() => expect(mockBrowseProcess).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "polymerization", page_size: 10 }),
      expect.any(AbortSignal)
    ));

    const separator = screen.getByRole("separator", { name: "调整记录抽屉宽度" });
    expect(separator.getAttribute("aria-valuenow")).toBe("380");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("396");

    fireEvent.click(screen.getByRole("button", { name: "关闭记录抽屉" }));
    const reopen = await screen.findByRole("button", { name: "重新打开记录" });
    fireEvent.click(reopen);
    expect(await screen.findByRole("dialog", { name: "原始记录" })).not.toBeNull();
  });

  it("记录抽屉支持搜索和分页且持续携带当前上下文", async () => {
    mockBrowseProcess.mockResolvedValue({
      ...emptyBrowse<ExperimentalProcessRecord>([
        {
          source_file: "process.csv",
          source_row_number: 1,
          polymer_id: "PI-1",
          polymer_name: "polyimide",
          product_name: "PI film",
          process_flow_original_text: "polymerization at 180 C",
          material_original_text: "NMP and PMDA"
        }
      ]),
      total_records: 25,
      matched_records: 25
    });
    renderAnalysis("process");
    fireEvent.click(await screen.findByRole("button", { name: /polymerization/ }));
    expect(await screen.findByText("PI film")).not.toBeNull();

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "NMP" } });
    await waitFor(() => expect(mockBrowseProcess).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "NMP", page: 1 }),
      expect.any(AbortSignal)
    ));

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(mockBrowseProcess).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "NMP", page: 2 }),
      expect.any(AbortSignal)
    ));
  });

  it("DFT 使用真实 PCA 数据并支持页签和散点键盘操作", async () => {
    const script = document.createElement("script");
    script.id = "3dmol-script";
    script.dataset.loaded = "true";
    document.head.appendChild(script);
    Object.assign(window, {
      $3Dmol: {
        createViewer: () => ({ addModel: vi.fn(), setStyle: vi.fn(), zoomTo: vi.fn(), render: vi.fn() })
      }
    });

    renderAnalysis("dft");
    const firstPoint = await screen.findByRole("button", { name: /DFT-1，PCA/ });
    expect(firstPoint.getAttribute("aria-pressed")).toBe("true");
    fireEvent.keyDown(firstPoint, { key: "ArrowRight" });
    await waitFor(() => expect(screen.getByRole("button", { name: /DFT-2，PCA/ }).getAttribute("aria-pressed")).toBe("true"));

    const analysisTab = screen.getByRole("tab", { name: "构象分析" });
    fireEvent.keyDown(analysisTab, { key: "ArrowRight" });
    expect((await screen.findByRole("tab", { name: "分子记录" })).getAttribute("aria-selected")).toBe("true");
    await waitFor(() => expect(mockBrowseDft).toHaveBeenCalled());
  });
});
