/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  KnowledgeSearchRequest,
  KnowledgeSearchResponse,
  OnlineKnowledgeSearchResponse
} from "../types";
import { KnowledgeSearch as KnowledgeSearchPage } from "./KnowledgeSearch";
import { useState, type ComponentProps } from "react";
import { KnowledgeRecordingProvider } from "../hooks/useKnowledgeRecording";
import { KnowledgeRecordingControls } from "./knowledge-search/KnowledgeRecordingControls";

function KnowledgeSearch(props: ComponentProps<typeof KnowledgeSearchPage>) {
  const [local, setLocal] = useState(true);
  return <KnowledgeRecordingProvider>
    <KnowledgeRecordingControls localMode={local} />
    <KnowledgeSearchPage {...props} onLocalModeChange={setLocal} />
  </KnowledgeRecordingProvider>;
}

const apiMocks = vi.hoisted(() => ({
  searchKnowledge: vi.fn(),
  postKnowledgeObservation: vi.fn(),
  startKnowledgeRecording: vi.fn(),
  stopKnowledgeRecording: vi.fn(),
  summarizeKnowledgeRecording: vi.fn(),
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
    postKnowledgeObservation: apiMocks.postKnowledgeObservation,
    startKnowledgeRecording: apiMocks.startKnowledgeRecording,
    stopKnowledgeRecording: apiMocks.stopKnowledgeRecording,
    summarizeKnowledgeRecording: apiMocks.summarizeKnowledgeRecording,
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
  const groups = payload.groups?.length
    ? payload.groups
    : payload.terms?.length
      ? payload.terms.map((term) => ({ terms: [term] }))
      : [{ terms: [payload.query] }];
  return {
    query: payload.query,
    groups,
    terms: groups.flatMap((group) => group.terms),
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
  apiMocks.postKnowledgeObservation.mockResolvedValue({ event: "article.opened" });
  apiMocks.startKnowledgeRecording.mockImplementation((recording_id: string) => Promise.resolve({ recording_id }));
  apiMocks.stopKnowledgeRecording.mockResolvedValue({
    recording_id: "recording", status: "stopped", started_at: "2026-09-09T01:00:00Z",
    ended_at: "2026-09-09T01:01:00Z", events: []
  });
  apiMocks.summarizeKnowledgeRecording.mockResolvedValue({ summary: "本次查看了文献 #1。", generated: true });
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
  it("输入框和空白区域输入 adad 都不启动记录，只能通过按钮开始", async () => {
    render(<KnowledgeSearch onBackHome={vi.fn()} />);
    expect((screen.getByRole("button", { name: "开始记录" }) as HTMLButtonElement).disabled).toBe(false);
    const input = screen.getByRole("searchbox", { name: "本地知识库检索词" });
    for (const key of "adad") fireEvent.keyDown(input, { key });
    for (const key of "adad") fireEvent.keyDown(window, { key, ctrlKey: true });
    for (const key of "adad") fireEvent.keyDown(window, { key, isComposing: true });
    for (const key of "adad") fireEvent.keyDown(window, { key, repeat: true });
    for (const key of "abab") fireEvent.keyDown(window, { key });
    input.focus();
    for (const key of "adad") fireEvent.keyDown(window, { key });
    expect(apiMocks.startKnowledgeRecording).not.toHaveBeenCalled();
    input.blur();
    for (const key of "adad") fireEvent.keyDown(window, { key });
    expect(apiMocks.startKnowledgeRecording).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
    await screen.findByRole("button", { name: "正在记录 · 总结" });
    for (const key of "adad") fireEvent.keyDown(window, { key });
    expect(apiMocks.startKnowledgeRecording).toHaveBeenCalledTimes(1);
  });

  it("开始按钮仅在本地模式可用，切换回来后可正常开始记录", async () => {
    const view = render(<KnowledgeSearch onBackHome={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "在线文献" }));
    expect((screen.getByRole("button", { name: "开始记录" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
    expect(apiMocks.startKnowledgeRecording).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: "本地知识库" }));
    expect((screen.getByRole("button", { name: "开始记录" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
    await screen.findByRole("button", { name: "正在记录 · 总结" });
    view.unmount();
    expect(apiMocks.startKnowledgeRecording).toHaveBeenCalledTimes(1);
  });

  it("同一记录关联搜索和查看，等待上报完成再结束并只显示总结", async () => {
    apiMocks.searchKnowledge.mockImplementation((payload: KnowledgeSearchRequest) => Promise.resolve({
      ...localResponse(payload), search_id: "search-recorded"
    }));
    const article = localResponse({ query: "polyimide", top_k: 20 }).results[0];
    apiMocks.stopKnowledgeRecording.mockResolvedValue({
      recording_id: "recording", status: "stopped", started_at: "2026-09-09T01:00:00Z",
      ended_at: "2026-09-09T01:01:00Z", events: [
        { sequence: 1, time: "2026-09-09T01:00:01Z", event: "search.completed", query: "polyimide", total: 1, page: 1 },
        { sequence: 2, time: "2026-09-09T01:00:02Z", event: "article.reaction_viewed", query: "polyimide", article }
      ]
    });
    render(<KnowledgeSearch onBackHome={vi.fn()} initialQuery="polyimide" />);
    await screen.findByRole("dialog", { name: "知识记录详情" });
    expect(apiMocks.searchKnowledge.mock.calls[0][0].recording_id).toBeUndefined();
    fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
    await screen.findByRole("button", { name: "正在记录 · 总结" });
    const recordingId = apiMocks.startKnowledgeRecording.mock.calls[0][0];
    fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
    await waitFor(() => expect(apiMocks.searchKnowledge).toHaveBeenLastCalledWith(
      expect.objectContaining({ recording_id: recordingId }), expect.any(AbortSignal)));
    await screen.findByRole("dialog", { name: "知识记录详情" });
    let resolve!: (value: object) => void;
    apiMocks.postKnowledgeObservation.mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    fireEvent.click(screen.getByRole("tab", { name: "反应信息" }));
    expect(apiMocks.postKnowledgeObservation).toHaveBeenLastCalledWith({
      search_id: "search-recorded", knowledge_id: 17525, source: "reaction_tab", recording_id: recordingId
    });
    expect((screen.getByRole("button", { name: "正在记录 · 总结" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => resolve({ event: "article.reaction_viewed" }));
    fireEvent.click(screen.getByRole("button", { name: "正在记录 · 总结" }));
    const summary = within(await screen.findByRole("dialog", { name: "本次浏览总结" }));
    expect(await summary.findByText("本次查看了文献 #1。")).not.toBeNull();
    expect(summary.queryByText(/查看对应内容|个操作|关联检索/)).toBeNull();
    expect(apiMocks.stopKnowledgeRecording).toHaveBeenCalledWith(recordingId);
    fireEvent.click(screen.getByRole("button", { name: /聚酰亚胺的合成方法/ }));
    expect(apiMocks.postKnowledgeObservation.mock.lastCall?.[0].recording_id).toBeUndefined();
  });

  it("结束失败可重试同一记录，等待重试时不混入新操作", async () => {
    apiMocks.stopKnowledgeRecording.mockRejectedValueOnce(new Error("offline"));
    render(<KnowledgeSearch onBackHome={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
    fireEvent.click(await screen.findByRole("button", { name: "正在记录 · 总结" }));
    await screen.findByRole("button", { name: "重试结束记录" });
    const input = screen.getByRole("searchbox", { name: "本地知识库检索词" });
    fireEvent.change(input, { target: { value: "polyimide" } });
    fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
    await waitFor(() => expect(apiMocks.searchKnowledge).toHaveBeenCalled());
    expect(apiMocks.searchKnowledge.mock.lastCall?.[0].recording_id).toBeUndefined();
    fireEvent.click(screen.getByRole("button", { name: "重试结束记录" }));
    await screen.findByText("本次查看了文献 #1。");
    expect(apiMocks.stopKnowledgeRecording.mock.calls[0]).toEqual(apiMocks.stopKnowledgeRecording.mock.calls[1]);
  });

  it("结束后自动生成总结，失败保留记录并用同一记录重试", async () => {
    let reject!: (reason: Error) => void;
    apiMocks.summarizeKnowledgeRecording.mockReturnValueOnce(new Promise((_, fail) => { reject = fail; }));
    render(<KnowledgeSearch onBackHome={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "开始记录" }));
    fireEvent.click(await screen.findByRole("button", { name: "正在记录 · 总结" }));
    const generating = await screen.findByRole("button", { name: "查看生成进度" });
    expect((generating as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("dialog", { name: "本次浏览总结" })).not.toBeNull();
    await act(async () => reject(new Error("模型暂时不可用")));
    fireEvent.click(await screen.findByRole("button", { name: "重试总结" }));
    await screen.findByText("本次查看了文献 #1。");
    expect(apiMocks.stopKnowledgeRecording).toHaveBeenCalledTimes(1);
    expect(apiMocks.summarizeKnowledgeRecording.mock.calls[0]).toEqual(apiMocks.summarizeKnowledgeRecording.mock.calls[1]);
    expect(screen.queryByText(/尚未生成 AI 总结/)).toBeNull();
  });

  it("只在主动点击和重新打开时通知，并关联当前结果的检索 ID", async () => {
    apiMocks.searchKnowledge.mockImplementation((payload: KnowledgeSearchRequest) => Promise.resolve({
      ...localResponse(payload), search_id: `search-${payload.page || 1}`
    }));
    render(<KnowledgeSearch onBackHome={vi.fn()} initialQuery="polyimide" />);
    await screen.findByRole("dialog", { name: "知识记录详情" });
    expect(apiMocks.postKnowledgeObservation).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /聚酰亚胺的合成方法/ }));
    await waitFor(() => expect(apiMocks.postKnowledgeObservation).toHaveBeenLastCalledWith({
      search_id: "search-1", knowledge_id: 17525, source: "result_card"
    }));
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(await screen.findByRole("button", { name: "查看记录详情" }));
    await waitFor(() => expect(apiMocks.postKnowledgeObservation).toHaveBeenLastCalledWith({
      search_id: "search-1", knowledge_id: 17525, source: "drawer_reopen"
    }));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(screen.getByRole("dialog").textContent).toContain("17526"));
    expect(apiMocks.postKnowledgeObservation).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: /聚酰亚胺的合成方法/ }));
    await waitFor(() => expect(apiMocks.postKnowledgeObservation).toHaveBeenLastCalledWith({
      search_id: "search-2", knowledge_id: 17526, source: "result_card"
    }));
  });

  it("后端没有提供检索 ID 时保持普通详情行为且不发送通知", async () => {
    render(<KnowledgeSearch onBackHome={vi.fn()} initialQuery="polyimide" />);
    await screen.findByRole("dialog", { name: "知识记录详情" });
    fireEvent.click(screen.getByRole("button", { name: /聚酰亚胺的合成方法/ }));
    fireEvent.click(screen.getByRole("tab", { name: "反应信息" }));
    expect(apiMocks.postKnowledgeObservation).not.toHaveBeenCalled();
  });

  it("主动切入反应信息才通知，支持键盘切换并关联换页后的文献", async () => {
    apiMocks.searchKnowledge.mockImplementation((payload: KnowledgeSearchRequest) => Promise.resolve({
      ...localResponse(payload), search_id: `search-${payload.page || 1}`
    }));
    render(<KnowledgeSearch onBackHome={vi.fn()} initialQuery="polyimide" />);
    await screen.findByRole("dialog", { name: "知识记录详情" });
    expect(apiMocks.postKnowledgeObservation).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: "反应信息" }));
    expect(screen.getByText("dianhydride + diamine")).not.toBeNull();
    expect(apiMocks.postKnowledgeObservation).toHaveBeenLastCalledWith({
      search_id: "search-1", knowledge_id: 17525, source: "reaction_tab"
    });
    fireEvent.click(screen.getByRole("tab", { name: "反应信息" }));
    fireEvent.click(screen.getByRole("tab", { name: "原文与溯源" }));
    expect(apiMocks.postKnowledgeObservation).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByRole("tab", { name: "原文与溯源" }), { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "反应信息" }).getAttribute("aria-selected")).toBe("true");
    expect(apiMocks.postKnowledgeObservation).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(screen.getByRole("dialog").textContent).toContain("17526"));
    expect(apiMocks.postKnowledgeObservation).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("tab", { name: "反应信息" }));
    expect(apiMocks.postKnowledgeObservation).toHaveBeenLastCalledWith({
      search_id: "search-2", knowledge_id: 17526, source: "reaction_tab"
    });
  });

  it("反应信息通知失败仍展示配方且不自动重试", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    apiMocks.searchKnowledge.mockImplementation((payload: KnowledgeSearchRequest) => Promise.resolve({
      ...localResponse(payload), search_id: "search-reaction-failure"
    }));
    apiMocks.postKnowledgeObservation.mockRejectedValue(new Error("offline"));
    render(<KnowledgeSearch onBackHome={vi.fn()} initialQuery="polyimide" />);
    await screen.findByRole("dialog", { name: "知识记录详情" });
    fireEvent.click(screen.getByRole("tab", { name: "反应信息" }));
    expect(screen.getByText("dianhydride + diamine")).not.toBeNull();
    await waitFor(() => expect(console.warn).toHaveBeenCalled());
    expect(apiMocks.postKnowledgeObservation).toHaveBeenCalledTimes(1);
  });

  it("通知失败不阻止查看文章详情", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    apiMocks.searchKnowledge.mockImplementation((payload: KnowledgeSearchRequest) => Promise.resolve({
      ...localResponse(payload), search_id: "search-failed-notification"
    }));
    apiMocks.postKnowledgeObservation.mockRejectedValue(new Error("offline"));
    render(<KnowledgeSearch onBackHome={vi.fn()} initialQuery="polyimide" />);
    await screen.findByRole("dialog", { name: "知识记录详情" });
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: /聚酰亚胺的合成方法/ }));
    expect(screen.getByRole("dialog", { name: "知识记录详情" })).not.toBeNull();
    await waitFor(() => expect(console.warn).toHaveBeenCalled());
  });

  it("解析符号化 AND/OR、展示实时预览并阻止不完整表达式", async () => {
    const writeText = vi.fn();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText }
    });
    render(<KnowledgeSearch onBackHome={vi.fn()} />);
    const pageTitle = screen.getByRole("heading", { name: "知识检索" });
    expect(pageTitle.classList.contains("np-module-page-title")).toBe(true);
    expect(pageTitle.closest(".np-module-page")).not.toBeNull();
    const input = screen.getByRole("searchbox", { name: "本地知识库检索词" });

    fireEvent.change(input, {
      target: { value: "polyimide；NMP | N-methyl-2-pyrrolidone" }
    });

    const preview = screen.getByLabelText(
      "检索逻辑：polyimide；NMP | N-methyl-2-pyrrolidone"
    );
    expect(within(preview).getByText("AND")).not.toBeNull();
    expect(within(preview).getByText("OR")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
    await waitFor(() => expect(apiMocks.searchKnowledge).toHaveBeenCalledWith(
      expect.objectContaining({
        query: "polyimide；NMP | N-methyl-2-pyrrolidone",
        groups: [
          { terms: ["polyimide"] },
          { terms: ["NMP", "N-methyl-2-pyrrolidone"] }
        ]
      }),
      expect.any(AbortSignal)
    ));
    fireEvent.click(await screen.findByRole("button", { name: "复制检索词" }));
    expect(writeText).toHaveBeenCalledWith("polyimide；NMP | N-methyl-2-pyrrolidone");

    fireEvent.change(input, { target: { value: "polyimide；" } });
    expect(screen.getByRole("alert").textContent).toContain("逻辑符号前后必须有完整关键词");
    expect((screen.getByRole("button", { name: "运行检索" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("保留深链自动检索、分页设置和完整溯源详情", async () => {
    render(<KnowledgeSearch onBackHome={vi.fn()} initialQuery="polyimide" initialTerms={["polyimide"]} />);

    expect((await screen.findAllByText("聚酰亚胺的合成方法")).length).toBeGreaterThanOrEqual(1);
    expect(apiMocks.searchKnowledge).toHaveBeenCalledWith(
      expect.objectContaining({
        query: "polyimide",
        page: 1,
        page_size: 20,
        groups: [{ terms: ["polyimide"] }]
      }),
      expect.any(AbortSignal)
    );
    expect(screen.getByRole("dialog", { name: "知识记录详情" })).not.toBeNull();
    expect(screen.getByText("A complete polyimide abstract for traceability.")).not.toBeNull();
    expect(screen.getByText("已选中")).not.toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "原文与溯源" }));
    expect(await screen.findByText("polymer_knowledge.jsonl")).not.toBeNull();

    const resizer = screen.getByRole("separator", { name: "调整详情抽屉宽度" });
    fireEvent.keyDown(resizer, { key: "ArrowLeft" });
    expect(resizer.getAttribute("aria-valuenow")).toBe("390");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "知识记录详情" })).toBeNull();
    const reopenButton = await screen.findByRole("button", { name: "查看记录详情" });
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

  it("在 2K 视口使用放大的详情抽屉范围和键盘步进", async () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: query === "(min-width: 2000px) and (min-height: 1120px)",
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn()
      }))
    });

    render(<KnowledgeSearch onBackHome={vi.fn()} initialQuery="polyimide" />);

    const resizer = await screen.findByRole("separator", { name: "调整详情抽屉宽度" });
    expect(resizer.getAttribute("aria-valuemin")).toBe("480");
    expect(resizer.getAttribute("aria-valuemax")).toBe("720");
    expect(resizer.getAttribute("aria-valuenow")).toBe("540");

    fireEvent.keyDown(resizer, { key: "ArrowLeft" });
    expect(resizer.getAttribute("aria-valuenow")).toBe("564");
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

    const drawer = document.querySelector("#knowledge-panel-online .ks-detail-drawer")!;
    fireEvent.keyDown(window, { key: "Escape" });
    expect(drawer.getAttribute("data-motion-phase")).toBe("exiting");
    expect(screen.queryByRole("button", { name: "查看记录详情" })).toBeNull();
    const exitComplete = new Event("transitionend", { bubbles: true });
    Object.defineProperty(exitComplete, "propertyName", { value: "transform" });
    fireEvent(drawer, exitComplete);
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
