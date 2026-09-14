/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  PropertyFilterOption,
  PropertyFilterOptionsResponse,
  PropertyFilterSearchResponse
} from "../types";
import { resetPropertyFilterHistogramResourceForTests } from "../services/propertyFilterHistogramResource";
import { resetPropertyFilterOptionsResourceForTests } from "../services/propertyFilterOptionsResource";
import { DatabaseFilterPage } from "./DatabaseFilterPage";
import { KnowledgeRecordingProvider } from "../hooks/useKnowledgeRecording";
import { KnowledgeRecordingControls } from "./knowledge-search/KnowledgeRecordingControls";

const apiMocks = vi.hoisted(() => ({
  fetchOptions: vi.fn(),
  fetchHistogram: vi.fn(),
  search: vi.fn(),
  observe: vi.fn(),
  start: vi.fn(), stop: vi.fn(), summary: vi.fn()
}));

function installTwoKMedia(initialMatches: boolean) {
  let matches = initialMatches;
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const mediaQuery = {
    get matches() {
      return matches;
    },
    media: "(min-width: 2000px) and (min-height: 1120px)",
    onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => true
  } as MediaQueryList;
  vi.stubGlobal("matchMedia", vi.fn(() => mediaQuery));
  return {
    setMatches(nextMatches: boolean) {
      matches = nextMatches;
      const event = { matches, media: mediaQuery.media } as MediaQueryListEvent;
      listeners.forEach((listener) => listener(event));
    }
  };
}

function installWorkbenchContainerWidth(initialWidth: number) {
  let width = initialWidth;
  const observers = new Set<ResizeObserverCallback>();
  class ResizeObserverMock {
    constructor(private readonly callback: ResizeObserverCallback) {}
    observe() {
      observers.add(this.callback);
      this.callback([], this as unknown as ResizeObserver);
    }
    unobserve() {
      observers.delete(this.callback);
    }
    disconnect() {
      observers.delete(this.callback);
    }
  }
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => ({
    width,
    height: 900,
    top: 0,
    right: width,
    bottom: 900,
    left: 0,
    x: 0,
    y: 0,
    toJSON: () => ({})
  }));
  return {
    setWidth(nextWidth: number) {
      width = nextWidth;
      observers.forEach((observer) => observer([], {} as ResizeObserver));
    }
  };
}

vi.mock("../services/api", () => ({
  API_BASE_URL: "/api/v1",
  fetchPropertyFilterHistogram: apiMocks.fetchHistogram,
  fetchPropertyFilterOptions: apiMocks.fetchOptions,
  searchPropertyFilterRecords: apiMocks.search,
  postPropertyFilterObservation: apiMocks.observe,
  startKnowledgeRecording: apiMocks.start, stopKnowledgeRecording: apiMocks.stop, summarizeKnowledgeRecording: apiMocks.summary
}));

const optionsResponse: PropertyFilterOptionsResponse = {
  query_time_ms: 7,
  total_records: 615_159,
  mapped_records: 191_761,
  raw_records: 423_398,
  data_source: "postgres",
  source_status: "ready",
  source_message: null,
  options: [
    {
      filter_type: "standardized",
      option_key: "standardized:tg:C",
      label: "玻璃化转变温度 (Tg)",
      property_key: "tg",
      property_name: null,
      property_unit_clean: null,
      canonical_unit: "°C",
      rows: 45_160,
      unique_smiles: 32_010,
      min_value: -273,
      p5_value: -179.15,
      median_value: 140,
      p95_value: 488.15,
      max_value: 890
    },
    {
      filter_type: "standardized",
      option_key: "standardized:bandgap:eV",
      label: "带隙 (Bandgap)",
      property_key: "bandgap",
      property_name: null,
      property_unit_clean: null,
      canonical_unit: "eV",
      rows: 12_000,
      unique_smiles: 9_100,
      min_value: 0,
      p5_value: 0.4,
      median_value: 2.6,
      p95_value: 6.8,
      max_value: 12
    },
    {
      filter_type: "raw",
      option_key: "raw:Cv:cal/(g*C)",
      label: "Cv · cal/(g*C)",
      property_key: null,
      property_name: "Cv",
      property_unit_clean: "cal/(g*C)",
      canonical_unit: null,
      rows: 8_200,
      unique_smiles: 6_400,
      min_value: 0.01,
      p5_value: 0.08,
      median_value: 0.31,
      p95_value: 0.92,
      max_value: 2.4
    },
    {
      filter_type: "standardized",
      option_key: "standardized:relative_permittivity:",
      label: "相对介电常数",
      property_key: "relative_permittivity",
      property_name: null,
      property_unit_clean: null,
      canonical_unit: null,
      rows: 4_200,
      unique_smiles: 3_900,
      min_value: 1,
      p5_value: 1.8,
      median_value: 3.2,
      p95_value: 7.4,
      max_value: 32
    }
  ]
};

const successResponse: PropertyFilterSearchResponse = {
  query: "",
  page: 1,
  page_size: 25,
  query_time_ms: 22.4,
  total_records: 615_159,
  matched_records: 26,
  data_source: "postgres",
  source_status: "ready",
  source_message: null,
  results: [
    {
      smiles: "C(C)O",
      canonical_smiles: "CCO",
      polymer_name: "示例聚合物",
      matched_filters: 1,
      records: [
        {
          filter_record_id: 11,
          source_row_number: 201,
          polymer_name: "示例聚合物",
          smiles: "C(C)O",
          canonical_smiles: "CCO",
          property_category: "Thermal",
          property_name: "Tg",
          property_value: "148",
          property_value_num: 148,
          property_unit_raw: "C",
          property_unit_clean: "C",
          property_key: "tg",
          property_label: "玻璃化转变温度",
          canonical_value: 148,
          canonical_unit: "°C",
          unit_conversion_status: "already_standard",
          value_origin: "observed",
          label_source: "exp",
          reliable_score: 0.98,
          soft_quality_flags: "",
          duplicate_flag: "",
          filter_index: 0
        },
        {
          filter_record_id: 12,
          source_row_number: 202,
          polymer_name: "示例聚合物",
          smiles: "C(C)O",
          canonical_smiles: "CCO",
          property_category: "Thermal",
          property_name: "Tg",
          property_value: "150",
          property_value_num: 150,
          property_unit_raw: "C",
          property_unit_clean: "C",
          property_key: "tg",
          property_label: "玻璃化转变温度",
          canonical_value: 150,
          canonical_unit: "°C",
          unit_conversion_status: "already_standard",
          value_origin: "median",
          label_source: "sim",
          reliable_score: 0.9,
          soft_quality_flags: "review",
          duplicate_flag: "possible_duplicate",
          filter_index: 0
        }
      ]
    }
  ]
};

function histogramResponse(option: PropertyFilterOption) {
  const total = option.rows;
  const underflow = Math.floor(total * 0.05);
  const overflow = Math.floor(total * 0.05);
  const central = total - underflow - overflow;
  const weights = [1, 2, 4, 6, 4, 1];
  const weightedUnit = Math.floor(central / weights.reduce((sum, value) => sum + value, 0));
  const counts = weights.map((weight) => weight * weightedUnit);
  counts[counts.length - 1] += central - counts.reduce((sum, value) => sum + value, 0);
  return {
    status: "success" as const,
    data: {
      query_time_ms: 4.2,
      option_key: option.option_key,
      data_source: "postgres",
      source_status: "ready",
      source_message: null,
      histogram: {
        domain_min: option.p5_value ?? option.min_value ?? 0,
        domain_max: option.p95_value ?? option.max_value ?? 0,
        domain_kind: "p5_p95" as const,
        bin_count: counts.length,
        counts,
        underflow_count: underflow,
        overflow_count: overflow,
        total_count: total
      }
    },
    etag: `W/"histogram-${option.option_key}"`
  };
}

beforeEach(() => {
  resetPropertyFilterHistogramResourceForTests();
  resetPropertyFilterOptionsResourceForTests();
  apiMocks.fetchOptions.mockReset().mockResolvedValue({
    status: "success",
    data: optionsResponse,
    etag: 'W/"pf-options-v1-test"'
  });
  apiMocks.fetchHistogram.mockReset().mockImplementation((optionKey: string) => {
    const option = optionsResponse.options.find((candidate) => candidate.option_key === optionKey);
    if (!option) throw new Error(`unknown histogram option ${optionKey}`);
    return Promise.resolve(histogramResponse(option));
  });
  apiMocks.search.mockReset().mockResolvedValue(successResponse);
  apiMocks.observe.mockReset().mockResolvedValue({});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function renderLoadedPage() {
  render(<DatabaseFilterPage />);
  await screen.findByRole("button", { name: /玻璃化转变温度/ });
}

describe("DatabaseFilterPage", () => {
  it("筛选与查看使用共同记录 ID，离开页面不取消已记录的筛选请求", async () => {
    apiMocks.start.mockImplementation(async (recording_id: string) => ({ recording_id, status: "recording" }));
    apiMocks.search.mockResolvedValue({ ...successResponse, search_id: "recorded" });
    const view = render(<KnowledgeRecordingProvider><KnowledgeRecordingControls localMode /><DatabaseFilterPage /></KnowledgeRecordingProvider>);
    await screen.findByRole("button", { name: /玻璃化转变温度/ });
    fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
    await screen.findByRole("button", { name: "正在记录 · 总结" });
    const id = apiMocks.start.mock.lastCall![0];
    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    fireEvent.click(await screen.findByText(/记录详情 · 2 条测量/));
    await waitFor(() => expect(apiMocks.observe).toHaveBeenCalledWith(expect.objectContaining({ recording_id: id })));
    expect(apiMocks.search.mock.lastCall![0].recording_id).toBe(id);
    let release!: (value: PropertyFilterSearchResponse) => void;
    apiMocks.search.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "120" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    const signal = apiMocks.search.mock.lastCall![1] as AbortSignal;
    view.rerender(<KnowledgeRecordingProvider><KnowledgeRecordingControls localMode /><div>另一模块</div></KnowledgeRecordingProvider>);
    expect(signal.aborted).toBe(false);
    expect((screen.getByRole("button", { name: "正在记录 · 总结" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => release(successResponse));
    await waitFor(() => expect((screen.getByRole("button", { name: "正在记录 · 总结" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it("只在展开记录或结构时通知，关闭与重复 toggle 不通知，再次展开仍可记录", async () => {
    apiMocks.search.mockResolvedValue({ ...successResponse, search_id: "first-search", results: [
      { ...successResponse.results[0], smiles: "*CC*", canonical_smiles: "*CO*" }
    ] });
    await renderLoadedPage();
    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    const summary = await screen.findByText(/记录详情 · 2 条测量/);
    expect(apiMocks.observe).not.toHaveBeenCalled();
    fireEvent.click(summary);
    await waitFor(() => expect(apiMocks.observe).toHaveBeenCalledTimes(1));
    expect(apiMocks.observe).toHaveBeenLastCalledWith({ search_id: "first-search", result_index: 0,
      source: "measurement_details", filter_index: 0 });
    fireEvent(summary.closest("details")!, new Event("toggle"));
    expect(apiMocks.observe).toHaveBeenCalledTimes(1);
    fireEvent.click(summary);
    await waitFor(() => expect(screen.queryByText("补充测量 1")).toBeNull());
    expect(apiMocks.observe).toHaveBeenCalledTimes(1);
    fireEvent.click(summary);
    await waitFor(() => expect(apiMocks.observe).toHaveBeenCalledTimes(2));
    for (const [label, field] of [["SMILES", "smiles"], ["canonical SMILES", "canonical_smiles"]]) {
      fireEvent.click(screen.getByText(label, { selector: ".dbf-smiles-summary-label strong" }).closest("summary")!);
      await waitFor(() => expect(apiMocks.observe).toHaveBeenLastCalledWith({ search_id: "first-search",
        result_index: 0, source: "smiles", smiles_field: field }));
    }
    apiMocks.search.mockResolvedValue({ ...successResponse, search_id: "second-search" });
    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "120" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    const secondSummary = await screen.findByText(/记录详情 · 2 条测量/);
    expect(apiMocks.observe).toHaveBeenCalledTimes(4);
    fireEvent.click(secondSummary);
    await waitFor(() => expect(apiMocks.observe).toHaveBeenLastCalledWith({ search_id: "second-search",
      result_index: 0, source: "measurement_details", filter_index: 0 }));
  });

  it("普通后端未返回快照 ID 时照常展开且不上报", async () => {
    await renderLoadedPage();
    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    fireEvent.click(await screen.findByText(/记录详情 · 2 条测量/));
    expect(await screen.findAllByText("原始测量")).toHaveLength(2);
    expect(apiMocks.observe).not.toHaveBeenCalled();
  });

  it("查看通知失败不会阻止详情展示或自动重试", async () => {
    apiMocks.search.mockResolvedValue({ ...successResponse, search_id: "failed-notice" });
    apiMocks.observe.mockRejectedValue(new Error("snapshot expired"));
    const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
    await renderLoadedPage();
    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    fireEvent.click(await screen.findByText(/记录详情 · 2 条测量/));
    expect(await screen.findAllByText("原始测量")).toHaveLength(2);
    await waitFor(() => expect(warning).toHaveBeenCalled());
    expect(apiMocks.observe).toHaveBeenCalledTimes(1);
  });

  it("使用共享工作台骨架，并将状态与 Surface 操作分层", async () => {
    await renderLoadedPage();

    const root = document.querySelector<HTMLElement>(".np-database-filter");
    expect(root?.classList.contains("np-structure-workbench")).toBe(true);
    expect(root?.classList.contains("np-material-discovery-page")).toBe(true);
    expect(
      screen.getByRole("heading", { name: "数据库筛选" }).classList.contains("np-material-discovery-page-title")
    ).toBe(true);
    expect(root?.querySelector(".np-sw-page > .np-sw-layout > .np-sw-workspace")).not.toBeNull();
    expect(root?.querySelectorAll(".dbf-module-toolbar button")).toHaveLength(0);

    const surfaceHeader = root?.querySelector<HTMLElement>(".dbf-surface-header");
    expect(surfaceHeader).not.toBeNull();
    expect(
      within(surfaceHeader as HTMLElement).getByRole("button", {
        name: "刷新筛选属性和分布统计"
      })
    ).not.toBeNull();
    expect(within(surfaceHeader as HTMLElement).getByRole("button", { name: "重置条件" })).not.toBeNull();
  });

  it("在原生 2K 档使用 540px 抽屉和 15px 键盘步进", async () => {
    installTwoKMedia(true);
    const container = installWorkbenchContainerWidth(2048);
    await renderLoadedPage();
    expect(document.querySelector<HTMLElement>(".np-database-filter")?.style.getPropertyValue("--np-sw-drawer-width")).toBe("540px");

    fireEvent.change(screen.getByLabelText("属性 1 最大值"), { target: { value: "180" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    expect(document.querySelector(".np-sw-drawer-layer")?.classList.contains("is-overlay")).toBe(true);
    act(() => container.setWidth(2050));
    await waitFor(() => expect(document.querySelector(".np-sw-drawer-layer")?.classList.contains("is-overlay")).toBe(false));
    const separator = await screen.findByRole("separator", { name: "调整筛选结果区域宽度" });
    expect(separator.getAttribute("aria-valuemin")).toBe("480");
    expect(separator.getAttribute("aria-valuemax")).toBe("720");
    expect(separator.getAttribute("aria-valuenow")).toBe("540");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("555");
  });

  it("跨标准与 2K 档时按可调范围比例映射抽屉宽度", async () => {
    const media = installTwoKMedia(false);
    await renderLoadedPage();
    fireEvent.change(screen.getByLabelText("属性 1 最大值"), { target: { value: "180" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    const separator = await screen.findByRole("separator", { name: "调整筛选结果区域宽度" });
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("390");

    act(() => media.setMatches(true));
    expect(separator.getAttribute("aria-valuemin")).toBe("480");
    expect(separator.getAttribute("aria-valuemax")).toBe("720");
    expect(separator.getAttribute("aria-valuenow")).toBe("550");
  });

  it("在 StrictMode 中合并目录请求，并在重新进入时同步使用缓存", async () => {
    const firstRender = render(
      <StrictMode>
        <DatabaseFilterPage />
      </StrictMode>
    );
    await screen.findByRole("button", { name: /玻璃化转变温度/ });
    await screen.findByRole("img", { name: /属性分布图/ });
    expect(apiMocks.fetchOptions).toHaveBeenCalledOnce();
    expect(apiMocks.fetchHistogram).toHaveBeenCalledOnce();

    firstRender.unmount();
    render(<DatabaseFilterPage />);
    expect(screen.getByRole("button", { name: /玻璃化转变温度/ })).not.toBeNull();
    expect(screen.queryByText("正在加载筛选属性")).toBeNull();
    expect(apiMocks.fetchOptions).toHaveBeenCalledOnce();
    expect(apiMocks.fetchHistogram).toHaveBeenCalledOnce();
  });

  it("目录缓存过期后先渲染表单，并在后台刷新失败时保留可用数据", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1_000);
    const firstRender = render(<DatabaseFilterPage />);
    await screen.findByRole("button", { name: /玻璃化转变温度/ });
    firstRender.unmount();

    now.mockReturnValue(61_001);
    apiMocks.fetchOptions.mockRejectedValueOnce(new Error("catalog refresh failed"));
    render(<DatabaseFilterPage />);

    expect(screen.getByRole("button", { name: /玻璃化转变温度/ })).not.toBeNull();
    expect(screen.queryByText("正在加载筛选属性")).toBeNull();
    expect(
      await screen.findByText("筛选属性更新失败，将继续使用已加载的数据。")
    ).not.toBeNull();
    expect(apiMocks.fetchOptions).toHaveBeenCalledTimes(2);
  });

  it("移除标题区性质分类标签，并可在 Surface 表头检查数据库更新", async () => {
    await renderLoadedPage();
    expect(document.querySelector(".dbf-surface-badges")).toBeNull();

    apiMocks.fetchOptions.mockResolvedValueOnce({
      status: "not-modified",
      data: null,
      etag: 'W/"pf-options-v1-test"'
    });
    fireEvent.click(screen.getByRole("button", { name: "刷新筛选属性和分布统计" }));

    await waitFor(() => expect(apiMocks.fetchOptions).toHaveBeenCalledTimes(2));
    expect(apiMocks.fetchOptions.mock.calls[1][0]).toMatchObject({
      etag: 'W/"pf-options-v1-test"'
    });
    expect(
      (await screen.findByRole("button", {
        name: "刷新筛选属性和分布统计"
      })) as HTMLButtonElement
    ).toHaveProperty("disabled", false);
  });

  it("优先选择 Tg，显示真实直方图，并按 standardized/raw 搜索分组", async () => {
    await renderLoadedPage();

    expect(screen.getByText("615,159")).not.toBeNull();
    expect(await screen.findByRole("img", { name: /属性分布图，45,160 条测量记录/ })).not.toBeNull();
    expect(screen.getByRole("heading", { name: "多属性范围筛选" })).not.toBeNull();
    expect(screen.getByText("需同时满足")).not.toBeNull();
    expect(screen.queryByText("当前草稿表达式")).toBeNull();
    expect(document.querySelector(".dbf-expression-capsule code")?.textContent).toContain("Tg：请填写最小值或最大值");
    expect(screen.getByText("相同聚合物合并展示")).not.toBeNull();
    expect(screen.queryByText(/PostgreSQL/)).toBeNull();
    const bars = document.querySelectorAll<HTMLElement>(".dbf-histogram-bars > i");
    expect(bars).toHaveLength(6);
    expect(Number.parseFloat(bars[0]?.style.height ?? "0")).toBeLessThan(
      Number.parseFloat(bars[3]?.style.height ?? "0")
    );
    expect(document.querySelector(".dbf-histogram-median")).not.toBeNull();
    expect(document.querySelector(".dbf-quantile-rail")).toBeNull();
    const histogramLabels = document.querySelectorAll(".dbf-histogram-labels span");
    expect(histogramLabels[0]?.textContent).toContain("-179.15");
    expect(histogramLabels[1]?.textContent).toContain("140");
    expect(histogramLabels[2]?.textContent).toContain("488.15");
    expect(apiMocks.fetchHistogram).toHaveBeenCalledOnce();
    expect((screen.getByLabelText("属性 1 最小值") as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("heading", { name: "筛选结果" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /玻璃化转变温度/ }));
    expect(screen.getByText("标准化属性")).not.toBeNull();
    expect(screen.getByText("原始属性")).not.toBeNull();
    fireEvent.change(screen.getByPlaceholderText("搜索属性名称或单位"), {
      target: { value: "Cv" }
    });
    expect(screen.queryByText("标准化属性")).toBeNull();
    expect(screen.getByText("原始属性")).not.toBeNull();
    expect(screen.getByRole("button", { name: /Cv · cal/ })).not.toBeNull();
  });

  it("禁用其它条件已使用的属性，并在属性全部占用后停止新增", async () => {
    await renderLoadedPage();

    fireEvent.click(screen.getByRole("button", { name: "添加条件" }));
    let conditionRows = document.querySelectorAll<HTMLElement>(".dbf-condition-row");
    expect(conditionRows).toHaveLength(2);

    const secondRow = conditionRows[1] as HTMLElement;
    const secondTrigger = secondRow.querySelector<HTMLButtonElement>(".dbf-property-trigger") as HTMLButtonElement;
    fireEvent.click(secondTrigger);
    const secondPicker = within(secondRow).getByRole("dialog", { name: "选择筛选属性" });
    const occupiedTg = within(secondPicker).getByRole("button", {
      name: /玻璃化转变温度.*已用于属性 1/
    }) as HTMLButtonElement;
    expect(occupiedTg.disabled).toBe(true);
    expect(occupiedTg.title).toBe("已用于属性 1");

    fireEvent.click(occupiedTg);
    expect(secondTrigger.textContent).toContain("带隙 (Bandgap)");
    expect(within(secondRow).getByRole("dialog", { name: "选择筛选属性" })).not.toBeNull();
    fireEvent.click(secondTrigger);

    const addButton = screen.getByRole("button", { name: "添加条件" });
    fireEvent.click(addButton);
    fireEvent.click(addButton);
    conditionRows = document.querySelectorAll<HTMLElement>(".dbf-condition-row");
    expect(conditionRows).toHaveLength(4);
    expect(screen.getByText("4 / 8")).not.toBeNull();
    expect(screen.getByText("所有可筛选属性均已添加")).not.toBeNull();
    expect((screen.getByRole("button", { name: "无更多可用属性" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("执行本地阈值校验并将真实 standardized 契约提交到结果抽屉", async () => {
    await renderLoadedPage();

    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    expect((await screen.findAllByText("请填写最小值或最大值。")).length).toBeGreaterThan(0);
    expect(apiMocks.search).not.toHaveBeenCalled();

    const minInput = screen.getByLabelText("属性 1 最小值");
    const maxInput = screen.getByLabelText("属性 1 最大值");
    fireEvent.change(minInput, { target: { value: "200" } });
    fireEvent.change(maxInput, { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    expect((await screen.findAllByText("最小值不能大于最大值。")).length).toBeGreaterThan(0);

    fireEvent.change(minInput, { target: { value: "100" } });
    fireEvent.change(maxInput, { target: { value: "200" } });
    fireEvent.change(screen.getByLabelText("关键词"), { target: { value: "x".repeat(201) } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    expect(await screen.findByText("关键词最多输入 200 个字符。")).not.toBeNull();
    expect(apiMocks.search).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("关键词"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));

    await waitFor(() => expect(apiMocks.search).toHaveBeenCalledOnce());
    expect(apiMocks.search).toHaveBeenCalledWith(
      {
        filters: [
          {
            filter_type: "standardized",
            property_key: "tg",
            canonical_unit: "°C",
            min_value: 100,
            max_value: 200
          }
        ],
        q: "",
        page: 1,
        page_size: 25
      },
      expect.any(AbortSignal)
    );

    expect(await screen.findByRole("heading", { name: "筛选结果" })).not.toBeNull();
    expect(screen.getByText("示例聚合物")).not.toBeNull();
    expect(screen.getByText("148 °C")).not.toBeNull();
    expect(screen.getByText(/记录详情 · 2 条测量/)).not.toBeNull();
    expect(screen.queryByText("原始测量")).toBeNull();
    fireEvent.click(screen.getByText(/记录详情 · 2 条测量/));
    expect(await screen.findAllByText("原始测量")).toHaveLength(2);

    const submittedContext = document.querySelector(".dbf-result-context strong");
    expect(submittedContext?.textContent).toContain("Tg：100–200 °C");
    fireEvent.change(minInput, { target: { value: "120" } });
    expect(document.querySelector(".dbf-expression-capsule code")?.textContent).toContain("Tg：120–200 °C");
    expect(submittedContext?.textContent).toContain("Tg：100–200 °C");

    const separator = screen.getByRole("separator", { name: "调整筛选结果区域宽度" });
    expect(separator.getAttribute("aria-valuenow")).toBe("380");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("390");

    fireEvent.click(screen.getByRole("button", { name: "关闭筛选结果" }));
    const reopen = await screen.findByRole("button", { name: /查看结果/ });
    expect(document.querySelector(".np-sw-drawer-layer")?.getAttribute("aria-hidden")).toBe("true");
    expect(screen.queryByRole("heading", { name: "筛选结果" })).toBeNull();
    expect(screen.getByText("示例聚合物").closest("[inert]")).not.toBeNull();
    fireEvent.click(reopen);
    expect(await screen.findByRole("heading", { name: "筛选结果" })).not.toBeNull();
    expect(apiMocks.search).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(apiMocks.search).toHaveBeenCalledTimes(2));
    expect(apiMocks.search.mock.calls[1][0]).toMatchObject({ page: 2, page_size: 25 });
  });

  it("切换属性会清空阈值，并为 raw 与无单位 standardized 选项发送精确单位", async () => {
    await renderLoadedPage();
    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "100" } });

    fireEvent.click(screen.getByRole("button", { name: /玻璃化转变温度/ }));
    fireEvent.click(screen.getByRole("button", { name: /Cv · cal/ }));
    expect((screen.getByLabelText("属性 1 最小值") as HTMLInputElement).value).toBe("");

    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "0.3" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    await waitFor(() => expect(apiMocks.search).toHaveBeenCalledOnce());
    expect(apiMocks.search.mock.calls[0][0].filters).toEqual([
      {
        filter_type: "raw",
        property_name: "Cv",
        property_unit_clean: "cal/(g*C)",
        min_value: 0.3,
        max_value: null
      }
    ]);

    fireEvent.click(screen.getByRole("button", { name: /Cv · cal/ }));
    fireEvent.click(screen.getByRole("button", { name: /相对介电常数/ }));
    fireEvent.change(screen.getByLabelText("属性 1 最大值"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    await waitFor(() => expect(apiMocks.search).toHaveBeenCalledTimes(2));
    expect(apiMocks.search.mock.calls[1][0].filters).toEqual([
      {
        filter_type: "standardized",
        property_key: "relative_permittivity",
        canonical_unit: "",
        min_value: null,
        max_value: 5
      }
    ]);
  });

  it("在结果抽屉中呈现错误、重试与空结果状态", async () => {
    apiMocks.search
      .mockRejectedValueOnce(new Error("database timeout"))
      .mockResolvedValueOnce({ ...successResponse, matched_records: 0, results: [] });
    await renderLoadedPage();

    fireEvent.change(screen.getByLabelText("属性 1 最大值"), { target: { value: "180" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    expect(await screen.findByText("暂时无法完成筛选")).not.toBeNull();
    expect(screen.getByText("筛选暂时无法完成，请稍后重试。")).not.toBeNull();
    expect(screen.queryByText("database timeout")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重新筛选" }));
    expect(await screen.findByText("没有找到匹配记录")).not.toBeNull();
    expect(apiMocks.search).toHaveBeenCalledTimes(2);
  });

  it("合并相同条件的进行中查询", async () => {
    let resolveSearch!: (response: PropertyFilterSearchResponse) => void;
    apiMocks.search.mockReturnValueOnce(
      new Promise<PropertyFilterSearchResponse>((resolve) => {
        resolveSearch = resolve;
      })
    );
    await renderLoadedPage();

    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    expect(document.querySelector(".dbf-result-skeletons")).not.toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "正在筛选" }));

    await waitFor(() => expect(apiMocks.search).toHaveBeenCalledOnce());
    resolveSearch(successResponse);
    expect(await screen.findByText("示例聚合物")).not.toBeNull();
    expect(apiMocks.search).toHaveBeenCalledOnce();
  });

  it("以单行省略预览收起长标题与 SMILES，并允许展开完整内容", async () => {
    const longLabel = `超长性质字段 ${"thermal_property_key_".repeat(8)}`;
    const longTitle = `poly{${"NCC(=O)c1ccc(cc1)".repeat(12)}}`;
    const longSmiles = `*${"CC(C)(C)OC(=O)NCC".repeat(18)}*`;
    apiMocks.fetchOptions.mockResolvedValueOnce({
      status: "success",
      data: {
        ...optionsResponse,
        options: [{ ...optionsResponse.options[0], label: longLabel }, ...optionsResponse.options.slice(1)]
      },
      etag: 'W/"pf-options-long-fields"'
    });
    apiMocks.search.mockResolvedValueOnce({
      ...successResponse,
      results: [{ ...successResponse.results[0], polymer_name: longTitle, canonical_smiles: longSmiles }]
    });

    render(<DatabaseFilterPage />);
    const longPropertyTrigger = await screen.findByRole("button", { name: new RegExp(longLabel.slice(0, 12)) });
    fireEvent.change(screen.getByLabelText("属性 1 最大值"), { target: { value: "180" } });
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));

    const title = await screen.findByRole("heading", { name: longTitle });
    const titleShell = title.closest(".dbf-result-title") as HTMLElement;
    const expandTitleButton = within(titleShell).getByRole("button", { name: "展开完整标题" });
    expect(titleShell.classList.contains("is-expanded")).toBe(false);
    expect(expandTitleButton.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(expandTitleButton);
    expect(titleShell.classList.contains("is-expanded")).toBe(true);
    expect(within(titleShell).getByRole("button", { name: "收起完整标题" }).getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(within(titleShell).getByRole("button", { name: "收起完整标题" }));
    expect(titleShell.classList.contains("is-expanded")).toBe(false);

    expect(document.querySelector(".dbf-smiles-content")).toBeNull();
    expect(document.querySelectorAll(".dbf-smiles-details")).toHaveLength(2);
    expect(screen.getByText("SMILES", { selector: ".dbf-smiles-summary-label strong" })).not.toBeNull();

    const canonicalLabel = screen.getByText("canonical SMILES", {
      selector: ".dbf-smiles-summary-label strong"
    });
    const canonicalDetails = canonicalLabel.closest("details") as HTMLDetailsElement;
    const canonicalSummary = canonicalLabel.closest("summary") as HTMLElement;
    const collapsedPreview = within(canonicalDetails).getByText(longSmiles, { selector: ".dbf-smiles-preview" });
    expect(canonicalDetails.open).toBe(false);
    expect(collapsedPreview.closest("summary")).toBe(canonicalSummary);
    expect(canonicalSummary.textContent).toContain("展开");
    expect(canonicalSummary.textContent).not.toContain("展开查看");

    fireEvent.click(canonicalSummary);
    const smiles = await screen.findByText(longSmiles, { selector: ".dbf-smiles-content code" });
    expect(within(canonicalDetails).queryByText(longSmiles, { selector: ".dbf-smiles-preview" })).toBeNull();
    expect(smiles.closest(".dbf-smiles-details")?.getAttribute("open")).not.toBeNull();
    expect(within(canonicalDetails).getByText("收起")).not.toBeNull();
    expect(within(canonicalDetails).getByRole("button", { name: "复制 canonical SMILES" })).not.toBeNull();

    fireEvent.click(canonicalSummary);
    await waitFor(() => expect(within(canonicalDetails).queryByText(longSmiles, { selector: ".dbf-smiles-content code" })).toBeNull());
    expect(within(canonicalDetails).getByText(longSmiles, { selector: ".dbf-smiles-preview" })).not.toBeNull();
    expect(canonicalDetails.open).toBe(false);
    expect(longPropertyTrigger.closest(".dbf-condition-property")).not.toBeNull();
    expect(document.querySelector(".dbf-result-card")?.closest(".dbf-drawer-body")).not.toBeNull();
  });

  it("限制最多八条条件，并在重置时取消进行中的查询", async () => {
    const expandedOptions: PropertyFilterOption[] = [
      ...optionsResponse.options,
      ...Array.from({ length: 4 }, (_, index) => ({
        ...optionsResponse.options[1],
        option_key: `standardized:test_property_${index + 5}:`,
        label: `测试属性 ${index + 5}`,
        property_key: `test_property_${index + 5}`,
        canonical_unit: null
      }))
    ];
    apiMocks.fetchOptions.mockResolvedValue({
      status: "success",
      data: { ...optionsResponse, options: expandedOptions },
      etag: 'W/"pf-options-eight-properties"'
    });
    apiMocks.fetchHistogram.mockImplementation((optionKey: string) => {
      const option = expandedOptions.find((candidate) => candidate.option_key === optionKey);
      if (!option) throw new Error(`unknown histogram option ${optionKey}`);
      return Promise.resolve(histogramResponse(option));
    });
    const requestSignals: AbortSignal[] = [];
    apiMocks.search.mockImplementation((_payload, signal: AbortSignal) => {
      requestSignals.push(signal);
      return new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    });
    await renderLoadedPage();

    const addButton = screen.getByRole("button", { name: "添加条件" });
    for (let index = 0; index < 7; index += 1) fireEvent.click(addButton);
    expect(screen.getByText("8 / 8")).not.toBeNull();
    expect((screen.getByRole("button", { name: "已达 8 条上限" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "100" } });
    const conditionRows = document.querySelectorAll(".dbf-condition-row");
    for (let index = 1; index < conditionRows.length; index += 1) {
      const input = within(conditionRows[index] as HTMLElement).getByRole("spinbutton", { name: `属性 ${index + 1} 最小值` });
      fireEvent.change(input, { target: { value: "1" } });
    }
    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    await waitFor(() => expect(apiMocks.search).toHaveBeenCalledOnce());
    expect(requestSignals[0]?.aborted).toBe(false);

    fireEvent.change(screen.getByLabelText("属性 1 最小值"), { target: { value: "101" } });
    fireEvent.click(screen.getByRole("button", { name: "正在筛选" }));
    await waitFor(() => expect(apiMocks.search).toHaveBeenCalledTimes(2));
    expect(requestSignals[0]?.aborted).toBe(true);
    expect(requestSignals[1]?.aborted).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "重置条件" }));
    await waitFor(() => expect(requestSignals[1]?.aborted).toBe(true));
    expect(screen.queryByRole("heading", { name: "筛选结果" })).toBeNull();
    expect(screen.getByText("1 / 8")).not.toBeNull();
  });

  it("呈现属性目录错误并允许重试", async () => {
    apiMocks.fetchOptions.mockRejectedValueOnce(new Error("PostgreSQL unavailable"));
    render(<DatabaseFilterPage />);

    expect(await screen.findByText("筛选属性加载失败，请稍后重试。")).not.toBeNull();
    expect(screen.queryByText("PostgreSQL unavailable")).toBeNull();
    apiMocks.fetchOptions.mockResolvedValueOnce({
      status: "success",
      data: optionsResponse,
      etag: 'W/"pf-options-v1-test"'
    });
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByRole("button", { name: /玻璃化转变温度/ })).not.toBeNull();
  });
});
