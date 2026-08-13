/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  KnowledgeSearchRequest,
  KnowledgeSearchResponse,
  OnlineKnowledgeSearchResponse
} from "../types";
import { KnowledgeSearch } from "./KnowledgeSearch";

const apiMocks = vi.hoisted(() => ({
  searchKnowledge: vi.fn(),
  fetchConfig: vi.fn(),
  fetchHistory: vi.fn(),
  createJob: vi.fn(),
  fetchJob: vi.fn(),
  deleteHistory: vi.fn(),
  clearHistory: vi.fn(),
  exportCsv: vi.fn()
}));

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    searchKnowledge: apiMocks.searchKnowledge,
    fetchOnlineKnowledgeDefaultConfig: apiMocks.fetchConfig,
    fetchOnlineKnowledgeHistory: apiMocks.fetchHistory,
    createOnlineKnowledgeJob: apiMocks.createJob,
    fetchOnlineKnowledgeJob: apiMocks.fetchJob,
    deleteOnlineKnowledgeHistory: apiMocks.deleteHistory,
    clearOnlineKnowledgeHistory: apiMocks.clearHistory,
    exportOnlineKnowledgeCsv: apiMocks.exportCsv
  };
});

function localResponse(payload: KnowledgeSearchRequest): KnowledgeSearchResponse {
  return {
    query: payload.query,
    terms: payload.terms?.length ? payload.terms : [payload.query],
    page: payload.page || 1,
    page_size: payload.page_size || 20,
    query_time_ms: 474.2,
    total: 3949,
    results: [{
      knowledge_id: (payload.page || 1) === 1 ? 17525 : 17526,
      source_file: "polymer_knowledge.jsonl",
      source_row_number: 17525,
      source_sequence: "SEQ-17525",
      title_zh: "聚酰亚胺的合成方法",
      title_en: "Method for preparing polyimide",
      abstract: "A complete polyimide abstract for traceability.",
      abstract_snippet: "A polyimide precursor is polymerized in a polar solvent.",
      claim: "A process for producing polyimide.",
      analysis: "The record describes a polymer synthesis reaction.",
      is_polymer_synthesis: "yes",
      judgement_reason: "Polymer formation and reaction conditions are explicit.",
      polymer_iupac: "polyimide",
      formulation: "dianhydride + diamine",
      catalyst: "triethylamine",
      temperature: "80 °C",
      reaction_time: "4 h",
      solvent: "NMP",
      matched_terms: ["polyimide"],
      matched_fields: ["Polymer", "Title"]
    }]
  };
}

const onlineResult: OnlineKnowledgeSearchResponse = {
  material: "PLA",
  mode: "property",
  query_time_ms: 37200,
  totalPapers: 8,
  max_papers: 20,
  exampleUsed: false,
  stats: { avgReliability: 78 },
  syntheses: [],
  propertyPoints: [{
    polymer_type: "biopolymer",
    polymer_name: "PLA",
    condition_name: "composition",
    condition_value: "10 wt%",
    property_name: "tensile strength",
    property_value: "62 MPa",
    relationship: "direct",
    paper_title: "Mechanical properties of PLA blends"
  }],
  temperatureDistribution: [],
  solventDistribution: [],
  catalystTable: [],
  tempLabels: [],
  conditionSummary: ["composition: 10 wt%"],
  reactionTypeTable: [],
  propertyNameDistribution: [{ label: "tensile strength", count: 1, percentage: 100 }],
  conditionDistribution: [{ label: "composition", count: 1, percentage: 100 }],
  polymerTypeDistribution: [{ label: "biopolymer", count: 1, percentage: 100 }],
  relationshipDistribution: [{ label: "direct", count: 1, percentage: 100 }],
  dataframe: [{ material: "PLA", property: "tensile strength" }]
};

beforeEach(() => {
  Object.values(apiMocks).forEach((mock) => mock.mockReset());
  apiMocks.searchKnowledge.mockImplementation((payload: KnowledgeSearchRequest) => Promise.resolve(localResponse(payload)));
  apiMocks.fetchConfig.mockResolvedValue({ base_url: "https://models.example/v1", model: "extractor", max_papers: 20, has_server_api_key: true });
  apiMocks.fetchHistory.mockResolvedValue({ history: [] });
  apiMocks.createJob.mockResolvedValue({ job_id: "online-job", status: "pending" });
  apiMocks.fetchJob.mockResolvedValue({
    job_id: "online-job",
    status: "completed",
    material: "PLA",
    mode: "property",
    max_papers: 20,
    progress_stage: "completed",
    progress_message: "Completed",
    processed_papers: 8,
    total_papers: 8,
    created_at: "2026-08-12T00:00:00Z",
    updated_at: "2026-08-12T00:00:37Z",
    error_message: null,
    result: onlineResult
  });
  apiMocks.deleteHistory.mockResolvedValue({ success: true });
  apiMocks.clearHistory.mockResolvedValue({ success: true });
  apiMocks.exportCsv.mockResolvedValue({ filename: "PLA_property_results.csv", csv_content: "material,property\nPLA,tensile strength" });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    }))
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("KnowledgeSearch", () => {
  it("保留深链自动检索、分页设置和完整溯源详情", async () => {
    render(<KnowledgeSearch onBackHome={vi.fn()} initialQuery="polyimide" initialTerms={["polyimide"]} />);

    expect((await screen.findAllByText("聚酰亚胺的合成方法")).length).toBeGreaterThanOrEqual(1);
    expect(apiMocks.searchKnowledge).toHaveBeenCalledWith(
      expect.objectContaining({ query: "polyimide", page: 1, page_size: 20, terms: ["polyimide"] }),
      expect.any(AbortSignal)
    );
    expect(screen.getByRole("dialog", { name: "知识记录详情" })).not.toBeNull();
    expect(screen.getByText("A complete polyimide abstract for traceability.")).not.toBeNull();
    expect(screen.getByText("已选中")).not.toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "原文与溯源" }));
    expect(screen.getByText("polymer_knowledge.jsonl")).not.toBeNull();

    const resizer = screen.getByRole("separator", { name: "调整详情抽屉宽度" });
    fireEvent.keyDown(resizer, { key: "ArrowLeft" });
    expect(resizer.getAttribute("aria-valuenow")).toBe("390");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "知识记录详情" })).toBeNull();
    const reopenButton = screen.getByRole("button", { name: "查看记录详情" });
    expect(reopenButton.classList.contains("is-vertical")).toBe(true);
    fireEvent.click(reopenButton);
    expect(screen.getByRole("dialog", { name: "知识记录详情" })).not.toBeNull();

    fireEvent.change(screen.getByRole("combobox", { name: "本地知识库每页数量" }), { target: { value: "50" } });
    await waitFor(() => expect(apiMocks.searchKnowledge).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, page_size: 50 }),
      expect.any(AbortSignal)
    ));

    fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
    await waitFor(() => expect(apiMocks.searchKnowledge).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 2, page_size: 50 }),
      expect.any(AbortSignal)
    ));
  });

  it("在线面板首次访问才加载，并在模式切换后保留表单状态", async () => {
    render(<KnowledgeSearch onBackHome={vi.fn()} />);
    expect(apiMocks.fetchConfig).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: /在线文献/ }));
    await waitFor(() => expect(apiMocks.fetchConfig).toHaveBeenCalledOnce());
    const onlinePanel = document.getElementById("knowledge-panel-online");
    expect(onlinePanel).not.toBeNull();
    expect(within(onlinePanel!).getByText("准备就绪")).not.toBeNull();
    expect(within(onlinePanel!).queryByText("结构化抽取说明")).toBeNull();
    const input = screen.getByRole("textbox", { name: "在线检索材料名称" });
    fireEvent.change(input, { target: { value: "PLA" } });

    fireEvent.click(screen.getByRole("tab", { name: /PDF 相似度/ }));
    fireEvent.click(screen.getByRole("tab", { name: /在线文献/ }));

    expect((screen.getByRole("textbox", { name: "在线检索材料名称" }) as HTMLInputElement).value).toBe("PLA");
    expect(apiMocks.fetchConfig).toHaveBeenCalledOnce();
  });

  it("在线性质任务展示真实结果并隐藏固定可靠度指标", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    render(<KnowledgeSearch onBackHome={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: /在线文献/ }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    fireEvent.change(screen.getByRole("textbox", { name: "在线检索材料名称" }), { target: { value: "PLA" } });
    fireEvent.click(screen.getByRole("button", { name: "开始检索" }));
    fireEvent.click(screen.getByRole("tab", { name: /本地知识库/ }));
    await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
    fireEvent.click(screen.getByRole("tab", { name: /在线文献/ }));

    expect(screen.getAllByText("tensile strength · 62 MPa").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("8 篇")).not.toBeNull();
    expect(screen.getByText("已选中")).not.toBeNull();
    expect(screen.queryByText(/Reliability|可靠度|78/)).toBeNull();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.getByRole("button", { name: "查看记录详情" }).classList.contains("is-vertical")).toBe(true);
  });

  it("PDF 只校验文件并在 950ms 后展示六篇固定结果", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const view = render(<KnowledgeSearch onBackHome={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: /PDF 相似度/ }));
    const pdfPanel = document.getElementById("knowledge-panel-pdf");
    expect(pdfPanel).not.toBeNull();
    expect(within(pdfPanel!).getByText("准备就绪")).not.toBeNull();
    expect(pdfPanel!.querySelector(".ks-module-toolbar .ks-toolbar-note")).toBeNull();
    expect(screen.getByText(/不读取、不上传 PDF 内容/)).not.toBeNull();

    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    fireEvent.change(input!, { target: { files: [new File(["text"], "notes.txt", { type: "text/plain" })] } });
    expect(screen.getByText("文件类型不受支持")).not.toBeNull();
    const file = new File(["not-read"], "polymer-review.pdf", { type: "application/pdf" });
    fireEvent.change(input!, { target: { files: [file] } });
    expect(screen.getByText("正在匹配固定示例论文")).not.toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(950); });

    const resultsSurface = screen.getByText("相似论文示例").closest("section");
    expect(resultsSurface).not.toBeNull();
    expect(within(resultsSurface!).getAllByRole("button", { name: /Similar Paper/ })).toHaveLength(6);
    expect(screen.getByRole("dialog", { name: "相似论文详情" })).not.toBeNull();
    expect(JSON.parse(window.localStorage.getItem("polyprop.pdfSimilarityDemo.uploadHistory") || "[]")).toHaveLength(1);
    expect(apiMocks.searchKnowledge).not.toHaveBeenCalled();
    expect(apiMocks.fetchConfig).not.toHaveBeenCalled();
  });

  it("删除正在等待恢复的 PDF 历史时取消演示定时器", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const view = render(<KnowledgeSearch onBackHome={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: /PDF 相似度/ }));

    const input = view.container.querySelector<HTMLInputElement>('input[type="file"]');
    fireEvent.change(input!, {
      target: { files: [new File(["not-read"], "pending.pdf", { type: "application/pdf" })] }
    });
    fireEvent.click(screen.getByRole("button", { name: /上传历史 1/ }));
    fireEvent.click(screen.getByRole("button", { name: "删除 pending.pdf" }));

    await act(async () => { await vi.advanceTimersByTimeAsync(950); });

    expect(screen.getByText("选择 PDF 预览相似论文")).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Similar Paper 1/ })).toBeNull();
    expect(JSON.parse(window.localStorage.getItem("polyprop.pdfSimilarityDemo.uploadHistory") || "[]")).toHaveLength(0);
  });
});
