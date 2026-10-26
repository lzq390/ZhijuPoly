/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KnowledgeSearchRequest, KnowledgeSearchResponse, LiteratureDetailResponse, LiteratureDocumentResult } from "../../types";
import { LocalKnowledgePanel } from "./LocalKnowledgePanel";

const mocks = vi.hoisted(() => ({ search: vi.fn(), sources: vi.fn(), detail: vi.fn(), fulltext: vi.fn(), observe: vi.fn() }));
vi.mock("../../services/api", async () => ({
  ...await vi.importActual<typeof import("../../services/api")>("../../services/api"),
  searchKnowledge: mocks.search, fetchKnowledgeSources: mocks.sources,
  fetchLiteratureDetail: mocks.detail, fetchLiteratureFullText: mocks.fulltext,
  postKnowledgeObservation: mocks.observe
}));

const paper: LiteratureDocumentResult = {
  data_source: "extracted_literature", knowledge_id: null, document_uid: "doc-one", generation_id: "generation-a", snapshot_uid: "snapshot-a",
  title: "Polyimide paper one", authors: ["A. Researcher"], journal: "Polymer Journal", year: 2021, doi: "10.5555/synthetic-ui-001",
  keywords: ["polyimide"], abstract: "A complete paper abstract.", abstract_snippet: "A polyimide abstract.",
  capabilities: { has_properties: true, has_processes: true, has_original: true, has_pdf: true, has_fulltext: true },
  counts: { samples: 2, properties: 2, processes: 1 }, matched_fields: ["title"], matched_terms: ["polyimide"]
};
const detail: LiteratureDetailResponse = {
  document: paper, generation_id: paper.generation_id, snapshot_uid: paper.snapshot_uid,
  extraction: { source_paper_id: "900001", source_extract_id: "900000000000000001", model_version: "extract-model", normalizer_version: "1" },
  quality_flags: [], capabilities: paper.capabilities,
  assets: [
    { asset_uid: "pdf-one", role: "pdf", filename: "paper.pdf", media_type: "application/pdf", status: "available", size_bytes: 1024 },
    { asset_uid: "text-one", role: "full_text", filename: "blocks.json", media_type: "application/json", status: "available", exact_input_verified: true },
    { asset_uid: "si-one", role: "attachment", filename: "supplement.zip", media_type: "application/zip", status: "unverified" }
  ],
  samples: [
    { sample_uid: "s1", name: "First polymer", identity_code: "PI-1", category: "polyimide", form: null,
      properties: [{ entry_uid: "p1", name: "Glass transition temperature", value: { single_value: 264, unit: "°C", min: null }, conditions: { method: "DSC" }, evidence: [{ quote: "PI-1 Tg 264 °C", block_id: "23" }] }],
      processes: [{ entry_uid: "r1", product_name: "PI-1", conditions: { temperature: { value: 180, unit: "°C" } }, post_treatment: null, evidence: [], materials: [
        { entry_uid: "m1", name: "ODPA", amounts: [{ kind: "mass", value: { value: 0.6796, unit: "g" } }], evidence: [] }
      ] }], characterization: null, evidence: [] },
    { sample_uid: "s2", name: "Second polymer", identity_code: "PI-2", properties: [{ entry_uid: "p2", name: "Glass transition temperature", value: { single_value: 300, unit: null }, conditions: null, evidence: [] }], processes: [], characterization: null, evidence: [] }
  ]
};

function response(payload: KnowledgeSearchRequest): KnowledgeSearchResponse {
  return {
    data_source: payload.data_source ?? "legacy", search_id: "search-a", generation_id: payload.data_source ? "generation-a" : undefined,
    query: payload.query, groups: payload.groups || [], terms: [payload.query], page: payload.page || 1, page_size: payload.page_size || 20, query_time_ms: 12,
    total: payload.data_source ? 80 : 1,
    results: payload.data_source ? [paper] : [{
      knowledge_id: 1, source_file: "legacy.jsonl", source_row_number: 1, source_sequence: null, title_zh: "原有聚合物知识", title_en: null,
      abstract: "legacy abstract", abstract_snippet: "legacy abstract", claim: null, analysis: null, is_polymer_synthesis: null, judgement_reason: null,
      polymer_iupac: null, formulation: null, catalyst: null, temperature: null, reaction_time: null, solvent: null, matched_terms: [], matched_fields: []
    }]
  };
}

beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.search.mockImplementation((payload: KnowledgeSearchRequest) => Promise.resolve(response(payload)));
  mocks.sources.mockResolvedValue({ sources: [{ data_source: "extracted_literature", enabled: true, ready: true, total: 80, generation_id: "generation-a" }] });
  mocks.detail.mockResolvedValue(detail);
  mocks.fulltext.mockResolvedValue({ blocks: [{ block_id: "23", type: "text", text: "PI-1 Tg 264 °C", parts: [{ type: "text", text: "PI-1 Tg 264 °C" }] }], next_cursor: null, exact_input_verified: true });
  mocks.observe.mockResolvedValue({ event: "article.opened" });
  Object.defineProperty(window, "matchMedia", { configurable: true, value: vi.fn().mockImplementation((query: string) => ({ matches: query === "(min-width: 1024px)", media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
});
afterEach(() => cleanup());

async function searchLiterature() {
  fireEvent.change(screen.getByLabelText("知识库数据来源"), { target: { value: "extracted_literature" } });
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "polyimide" } });
  fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
  await screen.findByRole("dialog", { name: "文献详情" });
  await waitFor(() => expect(mocks.detail).toHaveBeenCalled());
}

describe("extracted literature in local knowledge", () => {
  it("keeps legacy default/deep links and saves independent source drafts", async () => {
    const view = render(<LocalKnowledgePanel initialQuery="legacy" modeNavigation={null} />);
    await screen.findByRole("button", { name: /^原有聚合物知识/ });
    expect(mocks.search.mock.calls[0][0].data_source).toBeUndefined();
    await searchLiterature();
    fireEvent.change(screen.getByLabelText("文献起始年份"), { target: { value: "2020" } });
    fireEvent.change(screen.getByLabelText("知识库数据来源"), { target: { value: "legacy" } });
    expect((screen.getByRole("searchbox") as HTMLInputElement).value).toBe("legacy");
    expect(screen.queryByText("Polyimide paper one")).toBeNull();
    fireEvent.change(screen.getByLabelText("知识库数据来源"), { target: { value: "extracted_literature" } });
    expect((screen.getByRole("searchbox") as HTMLInputElement).value).toBe("polyimide");
    expect((screen.getByLabelText("文献起始年份") as HTMLInputElement).value).toBe("2020");
    fireEvent.click(screen.getByRole("button", { name: "重置条件" }));
    expect((screen.getByRole("searchbox") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("文献起始年份") as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByLabelText("本地知识库每页数量"), { target: { value: "50" } });
    view.rerender(<LocalKnowledgePanel initialQuery="new deep link" modeNavigation={null} />);
    await waitFor(() => expect(mocks.search.mock.calls.at(-1)?.[0]).toEqual(expect.objectContaining({ query: "new deep link", page: 1, page_size: 20 })));
    expect((screen.getByLabelText("知识库数据来源") as HTMLSelectElement).value).toBe("legacy");
    expect((screen.getByLabelText("本地知识库每页数量") as HTMLSelectElement).value).toBe("20");
  });

  it("pins pagination to submitted filters and generation while new drafts remain unsubmitted", async () => {
    render(<LocalKnowledgePanel modeNavigation={null} />);
    fireEvent.change(screen.getByLabelText("知识库数据来源"), { target: { value: "extracted_literature" } });
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "polyimide" } });
    fireEvent.change(screen.getByLabelText("文献起始年份"), { target: { value: "2020" } });
    fireEvent.click(screen.getByLabelText("有性质信息"));
    fireEvent.click(screen.getByLabelText("有可用原文"));
    fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
    await screen.findByRole("button", { name: "下一页" });
    fireEvent.change(screen.getByLabelText("文献起始年份"), { target: { value: "2025" } });
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(mocks.search).toHaveBeenLastCalledWith(expect.objectContaining({
      data_source: "extracted_literature", generation_id: "generation-a", page: 2,
      filters: { year_from: 2020, has_properties: true, has_original: true }
    }), expect.any(AbortSignal)));
    await screen.findByRole("button", { name: "下一页" });
    fireEvent.change(screen.getByLabelText("本地知识库每页数量"), { target: { value: "50" } });
    await waitFor(() => expect(mocks.search.mock.calls.at(-1)?.[0]).toEqual(expect.objectContaining({ page: 1, page_size: 50, generation_id: "generation-a", filters: { year_from: 2020, has_properties: true, has_original: true } })));
  });

  it("validates year bounds and preserves semicolons inside an exact DOI", async () => {
    render(<LocalKnowledgePanel modeNavigation={null} />);
    fireEvent.change(screen.getByLabelText("知识库数据来源"), { target: { value: "extracted_literature" } });
    const doi = "https://doi.org/10.1002/(sici)1097-4628(19961024)62:4<687::aid-app12>3.0.co;2-t";
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: doi } });
    fireEvent.change(screen.getByLabelText("文献起始年份"), { target: { value: "2025" } });
    fireEvent.change(screen.getByLabelText("文献结束年份"), { target: { value: "2020" } });
    expect(screen.getByRole("alert").textContent).toContain("起始年份不能晚于结束年份");
    expect((screen.getByRole("button", { name: "运行检索" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("文献结束年份"), { target: { value: "2026" } });
    fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
    await waitFor(() => expect(mocks.search.mock.calls[0][0].groups).toEqual([{ terms: [doi] }]));
  });

  it("separates sample properties/processes, preserves missing units and records only deliberate views", async () => {
    render(<LocalKnowledgePanel modeNavigation={null} />);
    await searchLiterature();
    expect(mocks.observe).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /^Polyimide paper one/ }));
    fireEvent.click(screen.getByRole("tab", { name: "已提取信息" }));
    expect(await screen.findByText("264")).toBeTruthy();
    expect(screen.getByText("0.6796")).toBeTruthy();
    expect(screen.getByText("ODPA")).toBeTruthy();
    expect(screen.queryByText("300")).toBeNull();
    fireEvent.change(screen.getByLabelText("文献样品"), { target: { value: "s2" } });
    expect(screen.getByText("300")).toBeTruthy();
    expect(screen.queryByText("264")).toBeNull();
    expect(screen.queryByText("ODPA")).toBeNull();
    expect(screen.queryByText("°C")).toBeNull();
    await waitFor(() => expect(mocks.observe).toHaveBeenCalledWith({ search_id: "search-a", document_uid: "doc-one", generation_id: "generation-a", snapshot_uid: "snapshot-a", source: "extracted_tab" }));
    expect(mocks.fulltext).not.toHaveBeenCalled();
  });

  it("loads safe full text lazily, links verified evidence and uses same-origin asset IDs", async () => {
    mocks.fulltext.mockResolvedValue({ blocks: [{ block_id: "23", type: "table", parts: [{ type: "table", rows: [["PI-1", "264", '<img src=x onerror="alert(1)">']] }] }], next_cursor: null, exact_input_verified: true });
    const view = render(<LocalKnowledgePanel modeNavigation={null} />);
    await searchLiterature();
    fireEvent.click(screen.getByRole("tab", { name: "已提取信息" }));
    fireEvent.click(await screen.findByText("查看原始依据（1）"));
    fireEvent.click(screen.getByRole("button", { name: "查看正文" }));
    expect(await screen.findByRole("cell", { name: "264" })).toBeTruthy();
    expect(screen.getByRole("cell", { name: '<img src=x onerror="alert(1)">' })).toBeTruthy();
    expect(view.container.querySelector("img")).toBeNull();
    expect(view.container.querySelector(".is-evidence-target")).not.toBeNull();
    const pdf = screen.getByRole("link", { name: "打开 PDF" });
    expect(pdf.getAttribute("href")).toBe("/api/v1/knowledge/literature/doc-one/files/pdf-one/content?generation_id=generation-a&snapshot_uid=snapshot-a&session_context=test-session");
    expect(pdf.getAttribute("target")).toBe("_blank");
    expect(screen.getByText("文件尚未验证")).toBeTruthy();
    expect(mocks.fulltext).toHaveBeenCalledWith("doc-one", "generation-a", "snapshot-a", undefined, expect.any(AbortSignal));
  });

  it("scrolls and focuses evidence when returning to full text that was already loaded", async () => {
    const scrollIntoView = vi.fn();
    const previousScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView });
    try {
      const view = render(<LocalKnowledgePanel modeNavigation={null} />);
      await searchLiterature();
      await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "关闭详情" })));
      fireEvent.click(screen.getByRole("tab", { name: "原文与来源" }));
      expect(await screen.findByText("PI-1 Tg 264 °C")).toBeTruthy();
      fireEvent.click(screen.getByRole("tab", { name: "已提取信息" }));
      fireEvent.click(await screen.findByText("查看原始依据（1）"));
      fireEvent.click(screen.getByRole("button", { name: "查看正文" }));

      await waitFor(() => {
        const target = view.container.querySelector(".is-evidence-target");
        expect(target).not.toBeNull();
        expect(document.activeElement).toBe(target);
        expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "smooth" });
      });
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(mocks.fulltext).toHaveBeenCalledTimes(1);
    } finally {
      if (previousScroll) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", previousScroll);
      else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
    }
  });

  it("cancels a pending search when changing source and ignores its eventual response", async () => {
    let resolve!: (value: KnowledgeSearchResponse) => void;
    mocks.search.mockImplementationOnce(() => new Promise<KnowledgeSearchResponse>((done) => { resolve = done; }));
    render(<LocalKnowledgePanel modeNavigation={null} />);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "legacy" } });
    fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
    const signal = mocks.search.mock.calls[0][1] as AbortSignal;
    fireEvent.change(screen.getByLabelText("知识库数据来源"), { target: { value: "extracted_literature" } });
    expect(signal.aborted).toBe(true);
    await act(async () => resolve(response({ query: "legacy", top_k: 20 })));
    expect(screen.queryByText("原有聚合物知识")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps metadata usable when structured details or original assets are unavailable", async () => {
    mocks.detail.mockRejectedValueOnce(new Error("文献详情暂不可用"));
    render(<LocalKnowledgePanel modeNavigation={null} />);
    await searchLiterature();
    expect(await screen.findByText("文献详情暂不可用")).toBeTruthy();
    const drawer = screen.getByRole("dialog", { name: "文献详情" });
    expect(within(drawer).getByText("A complete paper abstract.")).toBeTruthy();
    mocks.detail.mockResolvedValue({ ...detail, assets: [], capabilities: {} });
    fireEvent.click(within(drawer).getByRole("button", { name: "重试" }));
    await waitFor(() => expect(mocks.detail).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("tab", { name: "原文与来源" }));
    expect(await screen.findByText("未提供可读全文。")).toBeTruthy();
    expect(screen.getByText("未提供可访问的 PDF 或附件，已提取信息仍可查看。")).toBeTruthy();
    expect(mocks.fulltext).not.toHaveBeenCalled();
  });

  it("displays an offline literature source without treating it as an empty result", async () => {
    mocks.sources.mockResolvedValue({ sources: [{ data_source: "extracted_literature", enabled: true, ready: false, total: 0, reason: "文献服务离线" }] });
    render(<LocalKnowledgePanel modeNavigation={null} />);
    fireEvent.change(screen.getByLabelText("知识库数据来源"), { target: { value: "extracted_literature" } });
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "polyimide" } });
    expect(await screen.findByText("文献服务离线")).toBeTruthy();
    expect((screen.getByRole("button", { name: "运行检索" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByText("没有匹配的知识记录")).toBeNull();
    fireEvent.change(screen.getByLabelText("知识库数据来源"), { target: { value: "legacy" } });
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "legacy" } });
    fireEvent.click(screen.getByRole("button", { name: "运行检索" }));
    expect(await screen.findByRole("button", { name: /^原有聚合物知识/ })).toBeTruthy();
  });

  it("aborts stale details and prevents an older article from overwriting the selected one", async () => {
    let resolve!: (value: LiteratureDetailResponse) => void;
    mocks.detail.mockImplementationOnce(() => new Promise<LiteratureDetailResponse>((done) => { resolve = done; }));
    const second = { ...paper, document_uid: "doc-two", snapshot_uid: "snapshot-b", title: "Paper two" };
    mocks.search.mockImplementation((payload: KnowledgeSearchRequest) => Promise.resolve({ ...response(payload), results: [paper, second] }));
    mocks.detail.mockResolvedValue({ ...detail, document: { ...second, abstract: "Second article metadata" } });
    render(<LocalKnowledgePanel modeNavigation={null} />);
    await searchLiterature();
    const signal = mocks.detail.mock.calls[0][3] as AbortSignal;
    fireEvent.click(screen.getByRole("button", { name: /^Paper two/ }));
    expect(signal.aborted).toBe(true);
    expect(await screen.findByText("Second article metadata")).toBeTruthy();
    await act(async () => resolve(detail));
    expect(screen.getByText("Second article metadata")).toBeTruthy();
    expect(screen.queryByText("A complete paper abstract.")).toBeNull();
  });

  it("paginates full text within the selected snapshot and does not navigate unverified evidence", async () => {
    mocks.detail.mockResolvedValue({ ...detail, assets: detail.assets.map((asset) => ({ ...asset, exact_input_verified: false })) });
    mocks.fulltext.mockResolvedValueOnce({ blocks: [{ block_id: "1", type: "text", text: "First full text page" }], next_cursor: "100", exact_input_verified: false })
      .mockResolvedValueOnce({ blocks: [{ block_id: "23", type: "text", text: "Second full text page" }], next_cursor: null, exact_input_verified: false });
    const view = render(<LocalKnowledgePanel modeNavigation={null} />);
    await searchLiterature();
    fireEvent.click(screen.getByRole("tab", { name: "已提取信息" }));
    fireEvent.click(await screen.findByText("查看原始依据（1）"));
    expect(screen.queryByRole("button", { name: "查看正文" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "原文与来源" }));
    expect(await screen.findByText("First full text page")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "加载更多正文" }));
    expect(await screen.findByText("Second full text page")).toBeTruthy();
    expect(screen.getByText("First full text page")).toBeTruthy();
    expect(mocks.fulltext).toHaveBeenLastCalledWith("doc-one", "generation-a", "snapshot-a", "100", expect.any(AbortSignal));
    expect(view.container.querySelector(".is-evidence-target")).toBeNull();
  });
});
