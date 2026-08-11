/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  PropertyFilterOptionsResponse,
  PropertyFilterSearchResponse
} from "../types";
import { resetPropertyFilterOptionsResourceForTests } from "../services/propertyFilterOptionsResource";
import { DatabaseFilterPage } from "./DatabaseFilterPage";

const apiMocks = vi.hoisted(() => ({
  fetchOptions: vi.fn(),
  search: vi.fn()
}));

vi.mock("../services/api", () => ({
  API_BASE_URL: "/api/v1",
  fetchPropertyFilterOptions: apiMocks.fetchOptions,
  searchPropertyFilterRecords: apiMocks.search
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

beforeEach(() => {
  resetPropertyFilterOptionsResourceForTests();
  apiMocks.fetchOptions.mockReset().mockResolvedValue({
    status: "success",
    data: optionsResponse,
    etag: 'W/"pf-options-v1-test"'
  });
  apiMocks.search.mockReset().mockResolvedValue(successResponse);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

async function renderLoadedPage() {
  render(<DatabaseFilterPage />);
  await screen.findByRole("button", { name: /玻璃化转变温度/ });
}

describe("DatabaseFilterPage", () => {
  it("在 StrictMode 中合并目录请求，并在重新进入时同步使用缓存", async () => {
    const firstRender = render(
      <StrictMode>
        <DatabaseFilterPage />
      </StrictMode>
    );
    await screen.findByRole("button", { name: /玻璃化转变温度/ });
    expect(apiMocks.fetchOptions).toHaveBeenCalledOnce();

    firstRender.unmount();
    render(<DatabaseFilterPage />);
    expect(screen.getByRole("button", { name: /玻璃化转变温度/ })).not.toBeNull();
    expect(screen.queryByText("正在读取属性目录")).toBeNull();
    expect(apiMocks.fetchOptions).toHaveBeenCalledOnce();
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
    expect(screen.queryByText("正在读取属性目录")).toBeNull();
    expect(
      await screen.findByText("属性目录同步失败，当前继续使用本次会话中的缓存数据。")
    ).not.toBeNull();
    expect(apiMocks.fetchOptions).toHaveBeenCalledTimes(2);
  });

  it("移除标题区性质分类标签，并可在工具栏检查数据库更新", async () => {
    await renderLoadedPage();
    expect(document.querySelector(".dbf-surface-badges")).toBeNull();

    apiMocks.fetchOptions.mockResolvedValueOnce({
      status: "not-modified",
      data: null,
      etag: 'W/"pf-options-v1-test"'
    });
    fireEvent.click(screen.getByRole("button", { name: /刷新数据，检查数据库属性目录是否有更新/ }));

    await waitFor(() => expect(apiMocks.fetchOptions).toHaveBeenCalledTimes(2));
    expect(apiMocks.fetchOptions.mock.calls[1][0]).toMatchObject({
      etag: 'W/"pf-options-v1-test"'
    });
    expect(
      (await screen.findByRole("button", {
        name: /刷新数据，检查数据库属性目录是否有更新/
      })) as HTMLButtonElement
    ).toHaveProperty("disabled", false);
  });

  it("优先选择 Tg，显示真实五数分位，并按 standardized/raw 搜索分组", async () => {
    await renderLoadedPage();

    expect(screen.getByText("615,159")).not.toBeNull();
    expect(screen.getByText("45,160 条 · °C")).not.toBeNull();
    expect(screen.getByRole("img", { name: /45,160 条记录，32,010 个 SMILES/ })).not.toBeNull();
    expect(document.querySelector(".dbf-quantile-rail")).not.toBeNull();
    expect(document.querySelector(".dbf-quantile-bars")).toBeNull();
    const quantileLabels = document.querySelectorAll(".dbf-quantile-labels span");
    expect(quantileLabels[0]?.textContent).toContain("-179.15");
    expect(quantileLabels[1]?.textContent).toContain("140");
    expect(quantileLabels[2]?.textContent).toContain("488.15");
    expect((screen.getByLabelText("属性 1 最小值") as HTMLInputElement).value).toBe("");
    expect(screen.queryByRole("heading", { name: "筛选结果" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /玻璃化转变温度/ }));
    expect(screen.getByText("标准化属性")).not.toBeNull();
    expect(screen.getByText("原始属性")).not.toBeNull();
    fireEvent.change(screen.getByPlaceholderText("搜索属性名、key 或单位"), {
      target: { value: "Cv" }
    });
    expect(screen.queryByText("标准化属性")).toBeNull();
    expect(screen.getByText("原始属性")).not.toBeNull();
    expect(screen.getByRole("button", { name: /Cv · cal/ })).not.toBeNull();
  });

  it("执行本地阈值校验并将真实 standardized 契约提交到结果抽屉", async () => {
    await renderLoadedPage();

    fireEvent.click(screen.getByRole("button", { name: "运行筛选" }));
    expect((await screen.findAllByText("至少填写一个阈值。")).length).toBeGreaterThan(0);
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
    expect(submittedContext?.textContent).toContain("Tg 100–200 °C");
    fireEvent.change(minInput, { target: { value: "120" } });
    expect(document.querySelector(".dbf-expression-capsule code")?.textContent).toContain("Tg 120–200 °C");
    expect(submittedContext?.textContent).toContain("Tg 100–200 °C");

    const separator = screen.getByRole("separator", { name: "调整结果抽屉宽度" });
    expect(separator.getAttribute("aria-valuenow")).toBe("380");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("390");

    fireEvent.click(screen.getByRole("button", { name: "关闭筛选结果" }));
    const reopen = await screen.findByRole("button", { name: /查看结果/ });
    expect(reopen.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("示例聚合物")).toBeNull();
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
    expect(screen.getByText("database timeout")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "重试本次查询" }));
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
    fireEvent.click(await screen.findByRole("button", { name: "正在筛选" }));

    await waitFor(() => expect(apiMocks.search).toHaveBeenCalledOnce());
    resolveSearch(successResponse);
    expect(await screen.findByText("示例聚合物")).not.toBeNull();
    expect(apiMocks.search).toHaveBeenCalledOnce();
  });

  it("限制最多八条条件，并在重置时取消进行中的查询", async () => {
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

    expect(await screen.findByText("PostgreSQL unavailable")).not.toBeNull();
    apiMocks.fetchOptions.mockResolvedValueOnce({
      status: "success",
      data: optionsResponse,
      etag: 'W/"pf-options-v1-test"'
    });
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(await screen.findByRole("button", { name: /玻璃化转变温度/ })).not.toBeNull();
  });
});
