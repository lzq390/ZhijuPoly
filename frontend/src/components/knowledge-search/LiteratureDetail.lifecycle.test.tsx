/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LiteratureDetailResponse, LiteratureDocumentResult, LiteratureFullTextResponse } from "../../types";
import { LiteratureDetail } from "./LiteratureDetail";
import { useKnowledgeDrawerSizing } from "./KnowledgeDetailDrawer";

const api = vi.hoisted(() => ({ detail: vi.fn(), fulltext: vi.fn() }));
vi.mock("../../services/api", async () => ({
  ...await vi.importActual<typeof import("../../services/api")>("../../services/api"),
  fetchLiteratureDetail: api.detail, fetchLiteratureFullText: api.fulltext,
}));

// Independent synthetic metadata only; no source database or scientific result.
const paper: LiteratureDocumentResult = {
  data_source: "extracted_literature", knowledge_id: null,
  document_uid: "synthetic-document", generation_id: "synthetic-generation", snapshot_uid: "synthetic-snapshot",
  title: "Synthetic lifecycle document", authors: [], journal: null, year: null, doi: null,
  keywords: [], abstract: "Synthetic metadata", abstract_snippet: "", matched_fields: [], matched_terms: [],
  capabilities: { has_fulltext: true }, counts: {},
};
const detail: LiteratureDetailResponse = {
  document: paper, generation_id: paper.generation_id, snapshot_uid: paper.snapshot_uid,
  extraction: { source_paper_id: "synthetic-paper", source_extract_id: "synthetic-extract", normalizer_version: "synthetic" },
  assets: [], samples: [], quality_flags: [], capabilities: { has_fulltext: true },
};
function textPage(text: string, next: string | null = null): LiteratureFullTextResponse {
  return { blocks: [{ block_id: text, type: "text", text }], next_cursor: next, exact_input_verified: false };
}
function Pane({ open }: { open: boolean }) {
  const sizing = useKnowledgeDrawerSizing();
  return <LiteratureDetail record={paper} open={open} sizing={sizing} onClose={() => {}} onOpen={() => {}} onObserve={() => {}} />;
}
async function readyDetail() {
  await waitFor(() => expect(api.detail).toHaveBeenCalled());
  await waitFor(() => expect(screen.queryByText("正在加载文献详情…")).toBeNull());
}
beforeEach(() => {
  api.detail.mockReset().mockResolvedValue(detail);
  api.fulltext.mockReset().mockResolvedValue(textPage("Current synthetic page"));
  Object.defineProperty(window, "matchMedia", { configurable: true, value: vi.fn().mockImplementation((query: string) => ({ matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
});
afterEach(() => cleanup());

describe("literature drawer request lifecycle", () => {
  it("does not fetch while initially closed and retains completed detail on reopen", async () => {
    const view = render(<Pane open={false} />);
    expect(api.detail).not.toHaveBeenCalled();
    view.rerender(<Pane open />); await readyDetail();
    expect(api.detail).toHaveBeenCalledTimes(1);
    view.rerender(<Pane open={false} />);
    view.rerender(<Pane open />);
    await act(async () => {});
    expect(api.detail).toHaveBeenCalledTimes(1);
  });

  it("aborts pending detail on close, ignores its late reply and resumes only after reopen", async () => {
    let late!: (value: LiteratureDetailResponse) => void;
    api.detail.mockImplementationOnce(() => new Promise<LiteratureDetailResponse>((resolve) => { late = resolve; }));
    const view = render(<Pane open />);
    await waitFor(() => expect(api.detail).toHaveBeenCalledTimes(1));
    const signal = api.detail.mock.calls[0][3] as AbortSignal;
    fireEvent.click(screen.getByRole("tab", { name: "原文与来源" }));
    view.rerender(<Pane open={false} />);
    expect(signal.aborted).toBe(true);
    await act(async () => late(detail));
    expect(api.fulltext).not.toHaveBeenCalled();
    view.rerender(<Pane open />);
    await waitFor(() => expect(api.detail).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Current synthetic page")).toBeTruthy();
    expect(api.fulltext).toHaveBeenCalledTimes(1);
  });

  it("aborts initial full text on close and rejects its late response before resuming", async () => {
    let late!: (value: LiteratureFullTextResponse) => void;
    api.fulltext.mockImplementationOnce(() => new Promise<LiteratureFullTextResponse>((resolve) => { late = resolve; }));
    const view = render(<Pane open />); await readyDetail();
    fireEvent.click(screen.getByRole("tab", { name: "原文与来源" }));
    await waitFor(() => expect(api.fulltext).toHaveBeenCalledTimes(1));
    const signal = api.fulltext.mock.calls[0][4] as AbortSignal;
    view.rerender(<Pane open={false} />); expect(signal.aborted).toBe(true);
    await act(async () => late(textPage("Late obsolete page")));
    view.rerender(<Pane open />);
    expect(await screen.findByText("Current synthetic page")).toBeTruthy();
    expect(screen.queryByText("Late obsolete page")).toBeNull();
    expect(api.detail).toHaveBeenCalledTimes(1); expect(api.fulltext).toHaveBeenCalledTimes(2);
  });

  it("keeps loaded text and cursor across close/reopen without refetching", async () => {
    api.fulltext.mockResolvedValue(textPage("Retained synthetic page", "next-synthetic-page"));
    const view = render(<Pane open />); await readyDetail();
    fireEvent.click(screen.getByRole("tab", { name: "原文与来源" }));
    expect(await screen.findByText("Retained synthetic page")).toBeTruthy();
    view.rerender(<Pane open={false} />); view.rerender(<Pane open />);
    expect(await screen.findByText("Retained synthetic page")).toBeTruthy();
    expect(screen.getByRole("button", { name: "加载更多正文" })).toBeTruthy();
    expect(api.detail).toHaveBeenCalledTimes(1); expect(api.fulltext).toHaveBeenCalledTimes(1);
  });

  it("preserves loaded pages when a later page is aborted and retries only on deliberate load", async () => {
    let late!: (value: LiteratureFullTextResponse) => void;
    api.fulltext.mockResolvedValueOnce(textPage("Retained first page", "next-synthetic-page"))
      .mockImplementationOnce(() => new Promise<LiteratureFullTextResponse>((resolve) => { late = resolve; }))
      .mockResolvedValueOnce(textPage("Deliberate second page"));
    const view = render(<Pane open />); await readyDetail();
    fireEvent.click(screen.getByRole("tab", { name: "原文与来源" }));
    expect(await screen.findByText("Retained first page")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "加载更多正文" }));
    await waitFor(() => expect(api.fulltext).toHaveBeenCalledTimes(2));
    const signal = api.fulltext.mock.calls[1][4] as AbortSignal;
    view.rerender(<Pane open={false} />); expect(signal.aborted).toBe(true);
    await act(async () => late(textPage("Late second page")));
    view.rerender(<Pane open />);
    expect(await screen.findByText("Retained first page")).toBeTruthy();
    expect(screen.queryByText("Late second page")).toBeNull(); expect(api.fulltext).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "加载更多正文" }));
    expect(await screen.findByText("Deliberate second page")).toBeTruthy();
    expect(api.fulltext).toHaveBeenLastCalledWith(paper.document_uid, paper.generation_id, paper.snapshot_uid, "next-synthetic-page", expect.any(AbortSignal));
  });

  it("retains a successfully completed empty full-text response across reopening", async () => {
    api.fulltext.mockResolvedValue({ blocks: [], next_cursor: null, exact_input_verified: false });
    const view = render(<Pane open />); await readyDetail();
    fireEvent.click(screen.getByRole("tab", { name: "原文与来源" }));
    expect(await screen.findByText("全文暂无可展示内容。")).toBeTruthy();
    view.rerender(<Pane open={false} />); view.rerender(<Pane open />);
    await act(async () => {});
    expect(screen.getByText("全文暂无可展示内容。")).toBeTruthy();
    expect(api.fulltext).toHaveBeenCalledTimes(1);
  });

  it("rejects old detail after a reopened request has completed", async () => {
    let oldReply!: (value: LiteratureDetailResponse) => void;
    let currentReply!: (value: LiteratureDetailResponse) => void;
    api.detail.mockImplementationOnce(() => new Promise<LiteratureDetailResponse>((resolve) => { oldReply = resolve; }))
      .mockImplementationOnce(() => new Promise<LiteratureDetailResponse>((resolve) => { currentReply = resolve; }));
    const view = render(<Pane open />);
    await waitFor(() => expect(api.detail).toHaveBeenCalledTimes(1));
    const oldSignal = api.detail.mock.calls[0][3] as AbortSignal;
    view.rerender(<Pane open={false} />); view.rerender(<Pane open />);
    await waitFor(() => expect(api.detail).toHaveBeenCalledTimes(2));
    expect(oldSignal.aborted).toBe(true);
    await act(async () => currentReply({ ...detail, document: { ...paper, title: "Current detail metadata" } }));
    expect(await screen.findByText("Current detail metadata")).toBeTruthy();
    await act(async () => oldReply({ ...detail, document: { ...paper, title: "Obsolete detail metadata" } }));
    expect(screen.getByText("Current detail metadata")).toBeTruthy();
    expect(screen.queryByText("Obsolete detail metadata")).toBeNull();
  });

  it("keeps the reopened full-text request cancellable after an old request settles", async () => {
    let oldReply!: (value: LiteratureFullTextResponse) => void;
    let currentReply!: (value: LiteratureFullTextResponse) => void;
    api.fulltext.mockImplementationOnce(() => new Promise<LiteratureFullTextResponse>((resolve) => { oldReply = resolve; }))
      .mockImplementationOnce(() => new Promise<LiteratureFullTextResponse>((resolve) => { currentReply = resolve; }));
    const view = render(<Pane open />); await readyDetail();
    fireEvent.click(screen.getByRole("tab", { name: "原文与来源" }));
    await waitFor(() => expect(api.fulltext).toHaveBeenCalledTimes(1));
    const oldSignal = api.fulltext.mock.calls[0][4] as AbortSignal;
    view.rerender(<Pane open={false} />); view.rerender(<Pane open />);
    await waitFor(() => expect(api.fulltext).toHaveBeenCalledTimes(2));
    const currentSignal = api.fulltext.mock.calls[1][4] as AbortSignal;
    expect(oldSignal.aborted).toBe(true);
    await act(async () => oldReply(textPage("Obsolete first page")));
    expect(screen.queryByText("Obsolete first page")).toBeNull();
    expect(screen.getByText("正在加载全文…")).toBeTruthy();
    expect(currentSignal.aborted).toBe(false);
    view.rerender(<Pane open={false} />);
    expect(currentSignal.aborted).toBe(true);
    await act(async () => currentReply(textPage("Obsolete reopened page")));
    view.rerender(<Pane open />);
    expect(await screen.findByText("Current synthetic page")).toBeTruthy();
    expect(screen.queryByText("Obsolete reopened page")).toBeNull();
  });
});
