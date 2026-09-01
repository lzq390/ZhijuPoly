/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
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
import { useDatabaseRecordDrawerSizing } from "./database-analysis/DatabaseRecordDrawer";
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
    energyRange: { count: 30, min: -20, median: 120, max: 350 },
    gapRange: { count: 30, min: 0, median: 3.2, max: 8 },
    orbitalDistributions: [],
    stepRange: { count: 30, min: 1, median: 24, max: 41 },
    atomRange: { count: 30, min: 2, median: 18, max: 64 },
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
  vi.unstubAllGlobals();
  document.getElementById("3dmol-script")?.remove();
  delete window.$3Dmol;
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

function DrawerSizingProbe() {
  const sizing = useDatabaseRecordDrawerSizing();
  return (
    <>
      <output aria-label="抽屉宽度">{sizing.width}</output>
      <button type="button" onClick={() => sizing.setWidth(396)}>设置标准宽度</button>
    </>
  );
}

describe("数据库分析工作台", () => {
  it("以紧凑全库概览替代 Hero，并通过数据集浮层切换深链", async () => {
    const view = renderAnalysis();

    expect(await screen.findByText("数据集概览")).not.toBeNull();
    expect(screen.getByText("全库概览")).not.toBeNull();
    expect(screen.getByText("统计数据")).not.toBeNull();
    expect(screen.queryByText(/个真实数据源/)).toBeNull();
    expect(screen.queryByText("Polymer Data Platform")).toBeNull();
    const moduleToolbar = view.container.querySelector(".dba-module-toolbar");
    const toolbar = view.container.querySelector(".dba-toolbar");
    const surfaceHeader = view.container.querySelector(".dba-surface-head");
    expect(moduleToolbar).not.toBeNull();
    expect(toolbar).not.toBeNull();
    expect(moduleToolbar?.contains(toolbar)).toBe(false);
    expect(surfaceHeader?.contains(toolbar)).toBe(true);
    expect(view.container.querySelector(".dba-analysis-scroll")).not.toBeNull();
    expect(screen.getByText("汇总五类聚合物数据源，查看统计覆盖与数据状态")).not.toBeNull();
    expect(within(toolbar as HTMLElement).getAllByRole("button", { hidden: false })).toHaveLength(2);

    const trigger = screen.getByRole("button", { name: "选择数据集" });
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
    const refreshButton = screen.getByRole("button", { name: "刷新数据" });
    refreshButton.focus();
    fireEvent.pointerDown(refreshButton);
    expect(screen.queryByRole("region", { name: "选择数据集" })).toBeNull();
    expect(document.activeElement).toBe(refreshButton);
  });

  it("窄容器的数据集选择使用模态底部弹层并恢复焦点", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({
      width: 800,
      height: 700,
      top: 0,
      right: 800,
      bottom: 700,
      left: 0,
      x: 0,
      y: 0,
      toJSON: () => ({})
    }));
    renderAnalysis();
    await screen.findByText("数据集概览");
    const trigger = screen.getByRole("button", { name: "选择数据集" });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: "选择数据集" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.closest(".np-sw-workspace")?.classList.contains("has-dataset-modal")).toBe(true);
    expect(dialog.closest(".dba-surface-head")?.classList.contains("has-dataset-modal")).toBe(true);
    const first = within(dialog).getByRole("button", { name: /全库概览/ });
    const last = within(dialog).getByRole("button", { name: /配方比例数据/ });
    await waitFor(() => expect(document.activeElement).toBe(first));
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(screen.queryByRole("dialog", { name: "选择数据集" })).toBeNull();

    fireEvent.click(trigger);
    const reopenedDialog = await screen.findByRole("dialog", { name: "选择数据集" });
    const backdrop = screen.getByRole("button", { name: "关闭数据集选择" });
    fireEvent.pointerDown(backdrop);
    expect(reopenedDialog.isConnected).toBe(false);
    await waitFor(() => expect(document.activeElement).toBe(trigger));
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
    expect(screen.getByText("数据正在准备中，请稍后再试。")).not.toBeNull();
    expect(screen.queryByText("source is being prepared")).toBeNull();
    expect(screen.queryByText("过程关键词")).toBeNull();
  });

  it("数据字段和分类词条保持数据源原文", async () => {
    renderAnalysis("formulation");

    expect(await screen.findByText("Formula and dosage")).not.toBeNull();
    expect(screen.getByText("ratio colon")).not.toBeNull();
    expect(screen.getByText("120-179 C")).not.toBeNull();
    expect(screen.getByText("hours")).not.toBeNull();
    expect(screen.getAllByText("epoxy").length).toBeGreaterThan(0);
    expect(screen.getByText("DMF")).not.toBeNull();
    expect(screen.queryByText("配方与用量")).toBeNull();
    expect(screen.queryByText("冒号比例")).toBeNull();
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
    expect(await screen.findByText(/正在检查数据更新/)).not.toBeNull();
    expect(screen.getByText("过程关键词")).not.toBeNull();

    resolveRefresh?.({ ...analyticsResponse, source: "live", generated_at: null });
    expect(await screen.findByText(/数据已更新/)).not.toBeNull();
    expect(screen.getByText("已更新")).not.toBeNull();
    expect(mockSummary).toHaveBeenCalledTimes(2);
    expect(mockAnalytics.mock.invocationCallOrder[1]).toBeLessThan(mockSummary.mock.invocationCallOrder[1]);
  });

  it("数据表未变化时跳过重算并保留快照", async () => {
    mockAnalytics
      .mockResolvedValueOnce(analyticsResponse)
      .mockResolvedValueOnce({ ...analyticsResponse, refresh_status: "unchanged" });
    renderAnalysis("process");
    expect(await screen.findByText("过程关键词")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "刷新数据" }));

    expect(await screen.findByText(/数据已是最新/)).not.toBeNull();
    expect(screen.getByText("统计数据")).not.toBeNull();
    expect(screen.getByText("过程关键词")).not.toBeNull();
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
    expect(await screen.findByText(/数据已更新/)).not.toBeNull();
  });

  it("摘要失败时仍展示有效统计，并允许单独重试数据源状态", async () => {
    mockSummary.mockRejectedValueOnce(new Error("network failed"));
    renderAnalysis("process");

    expect(await screen.findByText("过程关键词")).not.toBeNull();
    expect(screen.getByText(/数据源状态暂不可用/)).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试状态" }));
    await waitFor(() => expect(mockSummary).toHaveBeenCalledTimes(2));
    expect(screen.getByText("过程关键词")).not.toBeNull();
  });

  it("格式异常的分析数据只隔离对应数据集并提供轻量重读", async () => {
    mockAnalytics.mockResolvedValueOnce({
      ...analyticsResponse,
      datasets: {
        ...analyticsDatasets,
        process: { ...analyticsDatasets.process, topTerms: undefined }
      }
    });
    renderAnalysis("process");

    expect(await screen.findByText("分析数据格式异常")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() => expect(mockAnalytics).toHaveBeenLastCalledWith(expect.objectContaining({ refresh: false })));
  });

  it("首次分析数据加载失败时隐藏内部消息并执行轻量重试", async () => {
    const backendError = Object.assign(new Error("后端统计快照暂不可用"), { name: "ApiRequestError", status: 503 });
    mockAnalytics.mockRejectedValueOnce(backendError).mockResolvedValueOnce(analyticsResponse);
    renderAnalysis("property");

    expect((await screen.findAllByText("数据服务暂不可用，请稍后重试。")).length).toBeGreaterThan(0);
    expect(screen.queryByText("后端统计快照暂不可用")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(screen.getByText("性能类别")).not.toBeNull());
    expect(mockAnalytics).toHaveBeenLastCalledWith(expect.objectContaining({ refresh: false }));
  });

  it("通用 HTTP 英文错误使用中文本地兜底", async () => {
    const transportError = Object.assign(new Error("Request failed with status 500"), { name: "ApiRequestError", status: 500 });
    mockAnalytics.mockRejectedValueOnce(transportError);
    renderAnalysis("property");

    expect((await screen.findAllByText("数据服务暂不可用，请稍后重试。")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Request failed with status 500")).toBeNull();
  });

  it("数组校验详情生成的英文 422 消息也使用中文兜底", async () => {
    const validationError = Object.assign(new Error("Request validation failed with status 422"), { name: "ApiRequestError", status: 422 });
    mockAnalytics.mockRejectedValueOnce(validationError);
    renderAnalysis("property");

    expect((await screen.findAllByText("请求条件不符合要求，请调整后重试。")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Request validation failed with status 422")).toBeNull();
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

    const requestCount = mockBrowseProcess.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "关闭记录抽屉" }));
    await waitFor(() => expect(document.activeElement).toBe(chartButton));
    const reopen = await screen.findByRole("button", { name: "重新打开记录" });
    fireEvent.click(reopen);
    expect(await screen.findByRole("dialog", { name: "原始记录" })).not.toBeNull();
    expect(mockBrowseProcess).toHaveBeenCalledTimes(requestCount);
    fireEvent.click(screen.getByRole("button", { name: "关闭记录抽屉" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "重新打开记录" })));
  });

  it("并排抽屉切换下钻后将焦点恢复到最近的图表触发器", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({
      width: 1600,
      height: 900,
      top: 0,
      right: 1600,
      bottom: 900,
      left: 0,
      x: 0,
      y: 0,
      toJSON: () => ({})
    }));
    renderAnalysis("process");
    const firstTrigger = await screen.findByRole("button", { name: /polymerization/ });
    const latestTrigger = screen.getByRole("button", { name: /Polyimide/ });
    fireEvent.click(firstTrigger);
    expect(await screen.findByRole("dialog", { name: "原始记录" })).not.toBeNull();
    fireEvent.click(latestTrigger);
    fireEvent.click(screen.getByRole("button", { name: "关闭记录抽屉" }));
    await waitFor(() => expect(document.activeElement).toBe(latestTrigger));
  });

  it("切换数据集时关闭并清理旧记录抽屉", async () => {
    const view = renderAnalysis("process");
    fireEvent.click(await screen.findByRole("button", { name: /polymerization/ }));
    expect(await screen.findByRole("dialog", { name: "原始记录" })).not.toBeNull();

    view.rerender(
      <DatabaseAnalysis
        selectedKey="property"
        onBackHome={vi.fn()}
        onBackDatabase={view.onBackDatabase}
        onOpenDataset={view.onOpenDataset}
      />
    );
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "原始记录" })).toBeNull());
    expect(screen.queryByRole("button", { name: "重新打开记录" })).toBeNull();
  });

  it("2K 档使用放大后的抽屉宽度、边界和键盘步进", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: true,
      media: "(min-width: 2000px) and (min-height: 1120px)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    })));
    renderAnalysis("process");
    fireEvent.click(await screen.findByRole("button", { name: /polymerization/ }));
    const separator = await screen.findByRole("separator", { name: "调整记录抽屉宽度" });
    expect(separator.getAttribute("aria-valuemin")).toBe("480");
    expect(separator.getAttribute("aria-valuemax")).toBe("720");
    expect(separator.getAttribute("aria-valuenow")).toBe("540");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("564");
  });

  it("2048 档使用覆盖抽屉，避免压缩双列分析区", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: true,
      media: "(min-width: 2000px) and (min-height: 1120px)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    })));
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({
      width: 1688,
      height: 1152,
      top: 0,
      right: 1688,
      bottom: 1152,
      left: 0,
      x: 0,
      y: 0,
      toJSON: () => ({})
    }));

    renderAnalysis("process");
    fireEvent.click(await screen.findByRole("button", { name: /polymerization/ }));
    const dialog = await screen.findByRole("dialog", { name: "原始记录" });
    await waitFor(() => expect(dialog.getAttribute("aria-modal")).toBe("true"));
    expect(screen.getByRole("separator", { name: "调整记录抽屉宽度" }).getAttribute("tabindex")).toBe("-1");
  });

  it("2560 档默认抽屉保持并排展示", async () => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      matches: true,
      media: "(min-width: 2000px) and (min-height: 1120px)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    })));
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({
      width: 2176,
      height: 1440,
      top: 0,
      right: 2176,
      bottom: 1440,
      left: 0,
      x: 0,
      y: 0,
      toJSON: () => ({})
    }));

    renderAnalysis("process");
    fireEvent.click(await screen.findByRole("button", { name: /polymerization/ }));
    const dialog = await screen.findByRole("dialog", { name: "原始记录" });
    await waitFor(() => expect(dialog.getAttribute("aria-modal")).toBe("false"));
    expect(screen.getByRole("separator", { name: "调整记录抽屉宽度" }).getAttribute("tabindex")).toBe("0");
  });

  it("跨越 2K 断点时按原区间比例映射抽屉宽度", async () => {
    let matches = false;
    let changeListener: ((event: MediaQueryListEvent) => void) | undefined;
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      get matches() { return matches; },
      media: "(min-width: 2000px) and (min-height: 1120px)",
      onchange: null,
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => { changeListener = listener; },
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    })));
    renderAnalysis("process");
    fireEvent.click(await screen.findByRole("button", { name: /polymerization/ }));
    const separator = await screen.findByRole("separator", { name: "调整记录抽屉宽度" });
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("396");

    matches = true;
    act(() => changeListener?.({ matches: true } as MediaQueryListEvent));
    await waitFor(() => expect(separator.getAttribute("aria-valuenow")).toBe("556"));
    expect(separator.getAttribute("aria-valuemin")).toBe("480");
    expect(separator.getAttribute("aria-valuemax")).toBe("720");
  });

  it("StrictMode 下跨越 2K 断点只映射一次抽屉宽度", async () => {
    let matches = false;
    let changeListener: ((event: MediaQueryListEvent) => void) | undefined;
    vi.stubGlobal("matchMedia", vi.fn(() => ({
      get matches() { return matches; },
      media: "(min-width: 2000px) and (min-height: 1120px)",
      onchange: null,
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => { changeListener = listener; },
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    })));
    render(<StrictMode><DrawerSizingProbe /></StrictMode>);
    fireEvent.click(screen.getByRole("button", { name: "设置标准宽度" }));
    expect(screen.getByRole("status", { name: "抽屉宽度" }).textContent).toBe("396");

    matches = true;
    act(() => changeListener?.({ matches: true } as MediaQueryListEvent));
    await waitFor(() => expect(screen.getByRole("status", { name: "抽屉宽度" }).textContent).toBe("556"));
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

  it("新下钻条件取消旧记录请求并阻止旧响应回写", async () => {
    let firstSignal: AbortSignal | undefined;
    mockBrowseProcess
      .mockImplementationOnce((_params, signal) => new Promise((_, reject) => {
        firstSignal = signal;
        signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      }))
      .mockResolvedValueOnce(emptyBrowse<ExperimentalProcessRecord>());
    renderAnalysis("process");
    fireEvent.click(await screen.findByRole("button", { name: /polymerization/ }));
    await waitFor(() => expect(mockBrowseProcess).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: /NMP/ }));
    await waitFor(() => expect(firstSignal?.aborted).toBe(true));
    await waitFor(() => expect(mockBrowseProcess).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: "NMP" }),
      expect.any(AbortSignal)
    ));
    expect(await screen.findByText("没有匹配记录")).not.toBeNull();
  });

  it("结果页数缩小时自动钳制页码并重新读取有效页", async () => {
    const firstPage = {
      ...emptyBrowse<ExperimentalProcessRecord>([]),
      total_records: 25,
      matched_records: 25
    };
    const emptyPage = emptyBrowse<ExperimentalProcessRecord>();
    mockBrowseProcess.mockResolvedValueOnce(firstPage).mockResolvedValue(emptyPage);
    renderAnalysis("process");
    fireEvent.click(await screen.findByRole("button", { name: /polymerization/ }));
    await screen.findByText(/第 1 \/ 3 页/);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));

    await waitFor(() => expect(mockBrowseProcess.mock.calls.length).toBeGreaterThanOrEqual(3));
    expect(mockBrowseProcess).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1 }),
      expect.any(AbortSignal)
    );
    expect(await screen.findByText(/第 1 \/ 1 页/)).not.toBeNull();
  });

  it("DFT 使用真实 PCA 数据并支持页签和散点键盘操作", async () => {
    const resize = vi.fn();
    const createViewer = vi.fn(() => ({ addModel: vi.fn(), setStyle: vi.fn(), zoomTo: vi.fn(), render: vi.fn(), clear: vi.fn(), resize, setBackgroundColor: vi.fn() }));
    const resizeCallbacks: ResizeObserverCallback[] = [];
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) { resizeCallbacks.push(callback); }
      observe() {}
      disconnect() {}
    });
    const script = document.createElement("script");
    script.id = "3dmol-script";
    script.dataset.loaded = "true";
    document.head.appendChild(script);
    Object.assign(window, {
      $3Dmol: {
        createViewer
      }
    });

    renderAnalysis("dft");
    const firstPoint = await screen.findByRole("button", { name: /DFT-1，PCA X/ });
    expect(mockPca).toHaveBeenCalledWith(80, expect.any(AbortSignal));
    await waitFor(() => expect(createViewer).toHaveBeenCalled());
    expect(firstPoint.getAttribute("aria-pressed")).toBe("true");
    fireEvent.keyDown(firstPoint, { key: "ArrowRight" });
    await waitFor(() => expect(screen.getByRole("button", { name: /DFT-2，PCA X/ }).getAttribute("aria-pressed")).toBe("true"));
    expect(screen.getByText("Step 0")).not.toBeNull();
    act(() => resizeCallbacks.forEach((callback) => callback([], {} as ResizeObserver)));
    expect(resize).toHaveBeenCalled();

    const analysisTab = screen.getByRole("tab", { name: "构象分析" });
    fireEvent.keyDown(analysisTab, { key: "ArrowRight" });
    expect((await screen.findByRole("tab", { name: "分子记录" })).getAttribute("aria-selected")).toBe("true");
    await waitFor(() => expect(mockBrowseDft).toHaveBeenCalled());
  });

  it("DFT 收敛状态保留原文并使用对应语义色", async () => {
    const record: DftMoleculeBrowserRecord = {
      mol_id: "DFT-NC-1",
      range_group: "small",
      final_step: 24,
      n_atoms: 12,
      trace_points: 25,
      scf_energy: -100,
      zero_point_energy: null,
      thermal_enthalpy: null,
      gibbs_free_energy: null,
      lowest_freq: null,
      dipole_moment: null,
      homo_ev: -5,
      lumo_ev: -2,
      gap_ev: 3,
      is_converged: "not_converged"
    };
    mockBrowseDft.mockResolvedValue({
      ...emptyBrowse([record]),
      total_step_records: 25,
      average_steps: 25,
      max_steps: 25
    });

    renderAnalysis("dft");
    fireEvent.click(await screen.findByRole("tab", { name: "分子记录" }));
    fireEvent.click(await screen.findByRole("button", { name: "DFT-NC-1" }));
    const status = await screen.findByText("not_converged");
    expect(status.classList.contains("is-danger")).toBe(true);
    expect(screen.queryByText("未收敛")).toBeNull();
    const drawer = screen.getByRole("dialog", { name: "原始记录" });
    expect(within(drawer).getByText("HOMO–LUMO gap")).not.toBeNull();
  });

  it("DFT 三维脚本失败后可重新加载并恢复渲染", async () => {
    renderAnalysis("dft");
    await screen.findByRole("button", { name: /DFT-1，PCA X/ });
    const failedScript = await waitFor(() => {
      const script = document.getElementById("3dmol-script") as HTMLScriptElement | null;
      expect(script).not.toBeNull();
      return script as HTMLScriptElement;
    });
    fireEvent.error(failedScript);
    const retry = await screen.findByRole("button", { name: "重试渲染" });

    const createViewer = vi.fn(() => ({
      addModel: vi.fn(),
      setStyle: vi.fn(),
      zoomTo: vi.fn(),
      render: vi.fn(),
      clear: vi.fn(),
      setBackgroundColor: vi.fn(),
      resize: vi.fn()
    }));
    window.$3Dmol = { createViewer };
    fireEvent.click(retry);
    const retryScript = await waitFor(() => {
      const script = document.getElementById("3dmol-script") as HTMLScriptElement | null;
      expect(script).not.toBeNull();
      expect(script).not.toBe(failedScript);
      return script as HTMLScriptElement;
    });
    fireEvent.load(retryScript);
    await waitFor(() => expect(createViewer).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "重试渲染" })).toBeNull();
  });
});
