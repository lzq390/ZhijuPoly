// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StructureWorkspaceContext } from "../types";
import type { TgAssistantSession } from "../hooks/useTgAssistant";
import { getTgAssistantSuggestions, ReverseDesignPage } from "./ReverseDesignPage";

describe("Tg assistant contextual recommendations", () => {
  const base = {
    isLoading: false,
    searchFailed: false,
    hasResultData: false,
    resultCount: 0,
    parametersDirty: false,
    editorReady: true,
    smilesState: "synced" as const,
    hasSmiles: true,
    validationMessage: null,
    targetTg: 450
  };

  it("recommends concrete design starting points for an empty canvas", () => {
    expect(getTgAssistantSuggestions({ ...base, hasSmiles: false })).toEqual([
      "为 450 °C 推荐一个可编辑的起始 SMILES",
      "哪些结构特征最可能帮助接近 450 °C？",
      "为首轮搜索建议相似度阈值和候选数量"
    ]);
  });

  it.each([
    [{ isLoading: true }, "根据当前扫描进度判断搜索是否正常"],
    [{ searchFailed: true }, "根据当前错误定位搜索失败原因"],
    [{ smilesState: "error" as const }, "检查当前 SMILES 为什么无效"],
    [{ validationMessage: "相似度阈值必须在 0–1 之间。" }, "解释并修正当前参数错误：相似度阈值必须在 0–1 之间。"],
    [{ hasResultData: true, parametersDirty: true }, "比较当前参数与上次搜索参数的差异"],
    [{ hasResultData: true, resultCount: 5 }, "比较当前页候选并给出优先验证顺序"],
    [{ hasResultData: true }, "分析本次没有候选的最可能原因"]
  ])("selects the recommendation set for page state %o", (overrides, firstSuggestion) => {
    expect(getTgAssistantSuggestions({ ...base, ...overrides })[0]).toBe(firstSuggestion);
  });
});

const mocks = vi.hoisted(() => ({
  submit: vi.fn(),
  reset: vi.fn(),
  setRequest: vi.fn(),
  resolveSmilesForSearch: vi.fn(),
  clearCanvas: vi.fn(),
  loadStructure: vi.fn(),
  importImageFile: vi.fn(),
  syncSmilesFromCanvas: vi.fn(),
  toggle3D: vi.fn(),
  copySmiles: vi.fn(),
  handleEditorLoad: vi.fn(),
  peekCanvasState: vi.fn(),
  standardizeSmiles: vi.fn(),
  reverseOverrides: {} as Record<string, unknown>
}));

vi.mock("../hooks/useReverseDesign", () => ({
  useReverseDesign: () => ({
    request: {
      target_tg: 450,
      smiles: "",
      similarity_threshold: 0.7,
      candidate_size: 200
    },
    setRequest: mocks.setRequest,
    submittedRequest: null,
    isLoading: false,
    error: null,
    data: null,
    job: null,
    submit: mocks.submit,
    reportError: vi.fn(),
    reset: mocks.reset,
    ...mocks.reverseOverrides
  })
}));

vi.mock("../services/api", () => ({
  standardizeSmiles: mocks.standardizeSmiles
}));

vi.mock("../hooks/useTgStructureCanvas", () => ({
  useTgStructureCanvas: () => ({
    fileInputRef: { current: null },
    handleEditorLoad: mocks.handleEditorLoad,
    isEditorReady: true,
    isFlipped: false,
    isFlipping: false,
    isImportingImage: false,
    isLoadingStructure: false,
    isClearing: false,
    isSyncing: false,
    isBusy: false,
    feedback: null,
    setFeedback: vi.fn(),
    copyState: "idle",
    clearCanvas: mocks.clearCanvas,
    loadStructure: mocks.loadStructure,
    importImageFile: mocks.importImageFile,
    syncSmilesFromCanvas: mocks.syncSmilesFromCanvas,
    toggle3D: mocks.toggle3D,
    peekCanvasState: mocks.peekCanvasState,
    resolveSmilesForSearch: mocks.resolveSmilesForSearch,
    copySmiles: mocks.copySmiles
  })
}));

vi.mock("./StructurePreview3D", () => ({
  StructurePreview3D: ({ smiles }: { smiles: string }) => (
    <div data-testid="structure-preview-3d">{smiles}</div>
  )
}));

function makeStructure(): StructureWorkspaceContext {
  return {
    smiles: "*CC*",
    setSmiles: vi.fn(),
    iframeRef: { current: null },
    setIsReady: vi.fn(),
    getCurrentSmiles: vi.fn().mockResolvedValue("*CC*")
  };
}

function makeAssistant(): TgAssistantSession {
  return {
    items: [],
    status: {
      enabled: false,
      configured: false,
      image: {
        supported: true,
        max_files: 2,
        max_canvas_snapshots: 1,
        max_user_upload_files: 1,
        max_bytes: 5 * 1024 * 1024,
        max_total_bytes: 10 * 1024 * 1024,
        accepted_mime_types: ["image/png", "image/jpeg", "image/webp"]
      }
    },
    guide: null,
    metadataLoading: false,
    metadataError: null,
    storageWarning: null,
    consent: "unknown",
    isStreaming: false,
    loadMetadata: vi.fn().mockResolvedValue(undefined),
    setConsent: vi.fn(),
    addDivider: vi.fn(),
    registerAdapter: vi.fn(() => vi.fn()),
    send: vi.fn().mockResolvedValue(true),
    retry: vi.fn(),
    stop: vi.fn(),
    newConversation: vi.fn(),
    rejectAction: vi.fn(),
    applyAction: vi.fn(),
    runNavigation: vi.fn()
  } as unknown as TgAssistantSession;
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.reverseOverrides = {};
  mocks.peekCanvasState.mockResolvedValue({
    smiles: "*CC*",
    canvasDirty: false,
    editorReady: true,
    viewMode: "2d",
    busy: false,
    revisionKey: "canvas-revision-1"
  });
  mocks.resolveSmilesForSearch.mockResolvedValue("*CC*");
  mocks.clearCanvas.mockResolvedValue(true);
  mocks.loadStructure.mockResolvedValue(true);
  mocks.standardizeSmiles.mockImplementation(async ({ smiles }: { smiles: string }) => ({
    input_smiles: smiles,
    standardized_smiles: smiles
  }));
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("ReverseDesignPage production workbench", () => {
  it("renders the root-level title and all six required toolbar controls", () => {
    const view = render(
      <ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={makeAssistant()} />
    );
    const title = screen.getByRole("heading", { name: "Tg 逆向设计" });

    expect(title.parentElement).toBe(view.container.firstElementChild);
    expect(screen.getByRole("button", { name: "导入图片" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "清空画布" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "生成SMILES" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "3D构象" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "搜索参数" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "AI 助手" })).toBeTruthy();

    fireEvent.load(screen.getByTitle("Tg 逆向设计结构编辑器"));
    expect(mocks.handleEditorLoad).toHaveBeenCalledOnce();
  });

  it("keeps parameters and AI mutually exclusive while allowing parameters over an open result drawer", async () => {
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={makeAssistant()} />);

    fireEvent.click(screen.getByRole("button", { name: "搜索参数" }));
    const parameterPanel = document.getElementById("tg-parameter-panel");
    expect(parameterPanel?.getAttribute("aria-hidden")).toBe("false");
    expect(screen.getAllByRole("spinbutton")).toHaveLength(3);

    fireEvent.click(screen.getByRole("button", { name: "AI 助手" }));
    expect(parameterPanel?.getAttribute("aria-hidden")).toBe("true");
    expect(document.getElementById("tg-assistant-panel")?.getAttribute("aria-hidden")).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "搜索参数" }));
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));

    await waitFor(() => {
      expect(mocks.submit).toHaveBeenCalledWith({
        target_tg: 450,
        smiles: "*CC*",
        similarity_threshold: 0.7,
        candidate_size: 200
      });
    });
    expect(document.querySelector(".tg-results-drawer")?.getAttribute("aria-hidden")).toBe("false");
    expect(parameterPanel?.getAttribute("aria-hidden")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "搜索参数" }));
    expect(parameterPanel?.getAttribute("aria-hidden")).toBe("false");
    expect(document.querySelector(".tg-results-drawer")?.getAttribute("aria-hidden")).toBe("false");
  });

  it("builds a minimal current-page snapshot without database IDs, job IDs, SVG, or raw errors", async () => {
    const results = Array.from({ length: 6 }, (_, index) => ({
      rank: index + 1,
      pi_id: 9000 + index,
      polymer_smiles: `*C${index}*`,
      canonical_polym: `*C${index}*`,
      monomer_a_smiles: `A${index}`,
      monomer_b_smiles: `B${index}`,
      monomer_a_iupac: `monomer A ${index}`,
      monomer_b_iupac: `monomer B ${index}`,
      monomer_a_structure_svg: `<svg id="a-${index}" />`,
      monomer_b_structure_svg: `<svg id="b-${index}" />`,
      tg_value: 450 + index,
      tg_unit: "°C",
      tg_difference: index,
      similarity_score: 0.9 - index * 0.01,
      structure_svg: `<svg id="polymer-${index}" />`,
      knowledge_available: true
    }));
    mocks.reverseOverrides = {
      submittedRequest: {
        target_tg: 450,
        smiles: "*CC*",
        similarity_threshold: 0.7,
        candidate_size: 200
      },
      error: "postgresql://secret@internal/raw stack trace",
      data: {
        target_tg: 450,
        query_time_ms: 12,
        candidate_pool_size: 6,
        sampled_candidate_count: 6,
        total: 6,
        data_source: "pi_reverse_design",
        results
      },
      job: {
        job_id: "private-job-id",
        status: "failed",
        scanned_rows: 120,
        matched_count: 6,
        current_tg_radius: 18.5,
        best_similarity_score: 0.9,
        message: "postgresql://secret@internal/raw stack trace"
      }
    };
    const assistant = makeAssistant();
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={assistant} />);
    await waitFor(() => expect(assistant.registerAdapter).toHaveBeenCalled());
    const registered = vi.mocked(assistant.registerAdapter).mock.calls.at(-1)?.[0];

    const context = await registered!.captureContext();
    const serialized = JSON.stringify(context);

    expect(context.result_view?.visible_candidates).toHaveLength(5);
    expect(context.error).toBe("Tg 搜索失败，请检查结构与服务状态后重试。");
    expect(serialized).not.toContain("pi_id");
    expect(serialized).not.toContain("private-job-id");
    expect(serialized).not.toContain("candidate_pool_size");
    expect(serialized).not.toContain("<svg");
    expect(serialized).not.toContain("postgresql://");
  });

  it("replaces invalid draft values with null and sends only a fixed validation reason", async () => {
    mocks.reverseOverrides = {
      request: {
        target_tg: 450,
        smiles: "",
        similarity_threshold: 9.87654321,
        candidate_size: 999
      }
    };
    const assistant = makeAssistant();
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={assistant} />);
    await waitFor(() => expect(assistant.registerAdapter).toHaveBeenCalled());
    const registered = vi.mocked(assistant.registerAdapter).mock.calls.at(-1)?.[0];

    const context = await registered!.captureContext();

    expect(context.draft_parameters).toEqual({
      target_tg: 450,
      similarity_threshold: null,
      candidate_size: null
    });
    expect(context.validation_error).toEqual({
      field: "similarity_threshold",
      message: "相似度阈值必须在 0–1 之间。"
    });
    expect(JSON.stringify(context)).not.toContain("9.87654321");
  });

  it("expires an action when the canvas changes after its snapshot", async () => {
    const assistant = makeAssistant();
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={assistant} />);
    await waitFor(() => expect(assistant.registerAdapter).toHaveBeenCalled());
    const registered = vi.mocked(assistant.registerAdapter).mock.calls.at(-1)?.[0];
    const snapshot = await registered!.captureContext();
    mocks.peekCanvasState.mockResolvedValue({
      smiles: "*CC*",
      canvasDirty: true,
      editorReady: true,
      viewMode: "2d",
      busy: false,
      revisionKey: "canvas-revision-2"
    });

    const result = await registered!.applyOperations(
      [{ type: "run_search" }],
      snapshot.action_context_revision
    );

    expect(result.status).toBe("expired");
    expect(mocks.resolveSmilesForSearch).not.toHaveBeenCalled();
    expect(mocks.submit).not.toHaveBeenCalled();
  });

  it("keeps an AI parameter patch but blocks submission when structure standardization fails", async () => {
    mocks.resolveSmilesForSearch.mockResolvedValue("");
    const assistant = makeAssistant();
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={assistant} />);
    await waitFor(() => expect(assistant.registerAdapter).toHaveBeenCalled());
    const registered = vi.mocked(assistant.registerAdapter).mock.calls.at(-1)?.[0];
    const snapshot = await registered!.captureContext();

    const result = await registered!.applyOperations(
      [
        { type: "set_parameters", parameters: { candidate_size: 50 } },
        { type: "run_search" }
      ],
      snapshot.action_context_revision
    );

    expect(result).toMatchObject({ status: "failed" });
    expect(mocks.setRequest).toHaveBeenCalledWith({
      target_tg: 450,
      smiles: "",
      similarity_threshold: 0.7,
      candidate_size: 50
    });
    expect(mocks.submit).not.toHaveBeenCalled();
  });

  it("debounces editable SMILES and loads the standardized structure without a submit button", async () => {
    vi.useFakeTimers();
    mocks.standardizeSmiles.mockResolvedValue({ input_smiles: "C(C)O", standardized_smiles: "CCO" });
    mocks.peekCanvasState.mockResolvedValue({
      smiles: "CCO",
      canvasDirty: false,
      editorReady: true,
      viewMode: "2d",
      busy: false,
      revisionKey: "canvas-cco"
    });
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={makeAssistant()} />);
    const input = screen.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" });

    fireEvent.change(input, { target: { value: "C(C)O" } });
    expect(mocks.standardizeSmiles).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /加载|提交|绘制 SMILES/ })).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    vi.useRealTimers();

    expect(mocks.standardizeSmiles).toHaveBeenCalledWith({ smiles: "C(C)O" });
    expect(mocks.loadStructure).toHaveBeenCalledWith("CCO", expect.objectContaining({ isCurrent: expect.any(Function) }));
  });

  it("keeps an invalid SMILES draft while preserving the existing canvas", async () => {
    vi.useFakeTimers();
    mocks.standardizeSmiles.mockRejectedValue(new Error("invalid"));
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={makeAssistant()} />);
    const input = screen.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" });

    fireEvent.change(input, { target: { value: "C(" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    vi.useRealTimers();

    expect((input as HTMLTextAreaElement).value).toBe("C(");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toContain("原画板未修改");
    expect(mocks.loadStructure).not.toHaveBeenCalled();
    expect(mocks.reset).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "3D构象" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("clears the canvas after an empty SMILES draft settles", async () => {
    vi.useFakeTimers();
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={makeAssistant()} />);
    const input = screen.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" });

    fireEvent.change(input, { target: { value: "" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    vi.useRealTimers();

    expect(mocks.clearCanvas).toHaveBeenCalledWith(expect.objectContaining({ isCurrent: expect.any(Function) }));
    expect(mocks.standardizeSmiles).not.toHaveBeenCalled();
  });

  it("keeps the old canvas and results when Ketcher rejects a standardized draft", async () => {
    vi.useFakeTimers();
    mocks.loadStructure.mockResolvedValue(false);
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={makeAssistant()} />);
    const input = screen.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" });

    fireEvent.change(input, { target: { value: "CCO" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    vi.useRealTimers();

    expect((input as HTMLTextAreaElement).value).toBe("CCO");
    expect(screen.getByRole("alert").textContent).toContain("未能同步到画板");
    expect(mocks.reset).not.toHaveBeenCalled();
  });

  it("adopts external canvas SMILES without writing it back into Ketcher", async () => {
    const assistant = makeAssistant();
    const structure = makeStructure();
    const view = render(
      <ReverseDesignPage structure={structure} onOpenKnowledge={vi.fn()} assistant={assistant} />
    );

    view.rerender(
      <ReverseDesignPage
        structure={{ ...structure, smiles: "CCC" }}
        onOpenKnowledge={vi.fn()}
        assistant={assistant}
      />
    );

    await waitFor(() => expect(
      (screen.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" }) as HTMLTextAreaElement).value
    ).toBe("CCC"));
    expect(mocks.loadStructure).not.toHaveBeenCalled();
  });

  it("expires an AI operation as soon as the SMILES draft revision changes", async () => {
    const assistant = makeAssistant();
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={assistant} />);
    await waitFor(() => expect(assistant.registerAdapter).toHaveBeenCalled());
    const registered = vi.mocked(assistant.registerAdapter).mock.calls.at(-1)?.[0];
    const snapshot = await registered!.captureContext();

    fireEvent.change(screen.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" }), {
      target: { value: "CCC" }
    });
    const result = await registered!.applyOperations(
      [{ type: "set_structure", smiles: "CCO" }],
      snapshot.action_context_revision
    );

    expect(result.status).toBe("expired");
    expect(mocks.loadStructure).not.toHaveBeenCalled();
  });

  it("applies latest-wins when a newer SMILES arrives during validation", async () => {
    vi.useFakeTimers();
    let resolveFirst: ((value: { input_smiles: string; standardized_smiles: string }) => void) | undefined;
    mocks.standardizeSmiles
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValueOnce({ input_smiles: "CCC", standardized_smiles: "CCC" });
    mocks.peekCanvasState.mockResolvedValue({
      smiles: "CCC",
      canvasDirty: false,
      editorReady: true,
      viewMode: "2d",
      busy: false,
      revisionKey: "canvas-ccc"
    });
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={makeAssistant()} />);
    const input = screen.getByRole("textbox", { name: "SMILES 输入，自动同步到画板" });

    fireEvent.change(input, { target: { value: "CC" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    fireEvent.change(input, { target: { value: "CCC" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    await act(async () => { resolveFirst?.({ input_smiles: "CC", standardized_smiles: "CC" }); });

    vi.useRealTimers();
    await waitFor(() => expect(mocks.loadStructure).toHaveBeenCalledTimes(1));
    expect(mocks.loadStructure.mock.calls[0][0]).toBe("CCC");
  });

  it("loads a confirmed AI set_structure operation through the shared canvas path", async () => {
    const assistant = makeAssistant();
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} assistant={assistant} />);
    await waitFor(() => expect(assistant.registerAdapter).toHaveBeenCalled());
    const registered = vi.mocked(assistant.registerAdapter).mock.calls.at(-1)?.[0];
    const snapshot = await registered!.captureContext();

    const result = await registered!.applyOperations(
      [{ type: "set_structure", smiles: "*CCO*" }],
      snapshot.action_context_revision
    );

    expect(result.status).toBe("applied");
    expect(mocks.loadStructure).toHaveBeenCalledWith("*CCO*");
    expect(mocks.setRequest).not.toHaveBeenCalled();
    expect(mocks.submit).not.toHaveBeenCalled();
  });
});
