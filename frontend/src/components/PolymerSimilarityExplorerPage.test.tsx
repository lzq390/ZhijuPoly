// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  SmilesQueryRequest,
  SmilesQueryResponse,
  StructureWorkspaceContext
} from "../types";
import { PolymerSimilarityExplorerPage } from "./PolymerSimilarityExplorerPage";
import type { StructureCanvasOwnerHandle } from "./StructureWorkbenchPage";

const mocks = vi.hoisted(() => ({
  canvasState: { isFlipped: false },
  loadStructure: vi.fn(),
  clearCanvas: vi.fn(),
  importImageFile: vi.fn(),
  syncSmilesFromCanvas: vi.fn(),
  resolveSmilesForSearch: vi.fn(),
  toggle3D: vi.fn(),
  copySmiles: vi.fn(),
  updateSmilesDraft: vi.fn(),
  flushSmilesDraft: vi.fn(),
  cancelSmilesDraftSync: vi.fn(),
  adoptCanvasSmiles: vi.fn(),
  handleEditorLoad: vi.fn()
}));

vi.mock("../hooks/useTgStructureCanvas", () => ({
  useTgStructureCanvas: ({ structure }: { structure: { smiles: string } }) => ({
    fileInputRef: { current: null },
    handleEditorLoad: mocks.handleEditorLoad,
    isEditorReady: true,
    isFlipped: mocks.canvasState.isFlipped,
    isFlipping: false,
    isImportingImage: false,
    isLoadingStructure: false,
    isClearing: false,
    isSyncing: false,
    isBusy: false,
    feedback: null,
    setFeedback: vi.fn(),
    copyState: "idle",
    smilesDraft: structure.smiles,
    smilesDraftState: "synced",
    smilesDraftError: null,
    updateSmilesDraft: mocks.updateSmilesDraft,
    flushSmilesDraft: mocks.flushSmilesDraft,
    cancelSmilesDraftSync: mocks.cancelSmilesDraftSync,
    adoptCanvasSmiles: mocks.adoptCanvasSmiles,
    loadStructure: mocks.loadStructure,
    clearCanvas: mocks.clearCanvas,
    importImageFile: mocks.importImageFile,
    syncSmilesFromCanvas: mocks.syncSmilesFromCanvas,
    resolveSmilesForSearch: mocks.resolveSmilesForSearch,
    toggle3D: mocks.toggle3D,
    copySmiles: mocks.copySmiles
  })
}));

vi.mock("./StructurePreview3D", () => ({
  StructurePreview3D: ({ smiles }: { smiles: string }) => (
    <div data-testid="similarity-structure-3d">{smiles}</div>
  )
}));

const DEFAULT_REQUEST: SmilesQueryRequest = {
  smiles: "*CC*",
  match_mode: "structure",
  similarity_threshold: 0.7,
  top_k: 10,
  property_name: null
};

const QUERY_DATA: SmilesQueryResponse = {
  match_type: "structure",
  query_time_ms: 12.3,
  total: 1,
  predicted_property_name: null,
  predicted_property_value: null,
  predicted_property_unit: null,
  results: [
    {
      polymer_id: "polymer-1",
      polymer_name: "示例聚合物",
      smiles: "CC",
      canonical_smiles: "CC",
      similarity_score: 0.91,
      structure_svg: null,
      matched_property_name: null,
      matched_property_value: null,
      matched_property_unit: null,
      matched_property_source: null,
      properties: {
        thermal: [],
        mechanical: [],
        electrical: [],
        chemical: [],
        optical: [],
        other: []
      }
    }
  ]
};

function makeStructure(smiles = "*CC*"): StructureWorkspaceContext {
  return {
    smiles,
    setSmiles: vi.fn(),
    iframeRef: { current: null },
    setIsReady: vi.fn(),
    getCurrentSmiles: vi.fn().mockResolvedValue(smiles)
  };
}

function renderPage(overrides: Partial<{
  structure: StructureWorkspaceContext;
  request: SmilesQueryRequest;
  queryData: SmilesQueryResponse | null;
  queryError: string | null;
  submitQuery: (request?: SmilesQueryRequest) => Promise<void>;
}> = {}) {
  const setRequest = vi.fn();
  const submitQuery = overrides.submitQuery ?? vi.fn().mockResolvedValue(undefined);
  const view = render(
    <PolymerSimilarityExplorerPage
      structure={overrides.structure ?? makeStructure()}
      request={overrides.request ?? DEFAULT_REQUEST}
      setRequest={setRequest}
      isQueryLoading={false}
      queryError={overrides.queryError ?? null}
      queryData={overrides.queryData === undefined ? QUERY_DATA : overrides.queryData}
      submitQuery={submitQuery}
    />
  );
  return { ...view, setRequest, submitQuery };
}

function openParameters() {
  fireEvent.click(screen.getByRole("button", { name: "探索参数" }));
  return screen.getByRole("dialog", { name: "相似性探索参数" });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.canvasState.isFlipped = false;
  mocks.loadStructure.mockResolvedValue(true);
  mocks.clearCanvas.mockResolvedValue(true);
  mocks.importImageFile.mockResolvedValue(true);
  mocks.syncSmilesFromCanvas.mockResolvedValue("*CC*");
  mocks.resolveSmilesForSearch.mockResolvedValue("*CC*");
  mocks.toggle3D.mockResolvedValue(true);
  mocks.flushSmilesDraft.mockResolvedValue(true);
  mocks.cancelSmilesDraftSync.mockResolvedValue(undefined);
});

afterEach(() => cleanup());

describe("PolymerSimilarityExplorerPage", () => {
  it("更名后复用结构工作台画板，并在首次运行前隐藏结果抽屉", () => {
    renderPage();

    expect(screen.getByRole("heading", { name: "聚合物相似性探索" })).toBeTruthy();
    expect(screen.getByTitle("聚合物相似性探索结构编辑器")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "AI 助手" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "相似性探索结果" })).toBeNull();
    expect(screen.queryByRole("button", { name: "展开相似性探索结果" })).toBeNull();
  });

  it("默认按固定参数运行结构相似检索并打开结果抽屉", async () => {
    const { setRequest, submitQuery } = renderPage();
    const panel = openParameters();

    expect((within(panel).getByRole("radio", { name: /结构相似/ }) as HTMLElement).getAttribute("aria-checked")).toBe("true");
    expect((within(panel).getByRole("spinbutton", { name: "相似度阈值" }) as HTMLInputElement).value).toBe("0.7");
    expect((within(panel).getByRole("spinbutton", { name: "搜索数量" }) as HTMLInputElement).value).toBe("10");
    fireEvent.click(within(panel).getByRole("button", { name: "运行探索" }));

    const expected: SmilesQueryRequest = {
      smiles: "*CC*",
      match_mode: "structure",
      similarity_threshold: 0.7,
      top_k: 10,
      property_name: null
    };
    await waitFor(() => expect(submitQuery).toHaveBeenCalledWith(expected));
    expect(setRequest).toHaveBeenCalledWith(expected);
    expect(screen.getByRole("dialog", { name: "相似性探索结果" })).toBeTruthy();
    expect(screen.getByText("示例聚合物")).toBeTruthy();
    expect(screen.getAllByText("0.910").length).toBeGreaterThan(0);
  });

  it("性能相似模式提交所选性质并展示预测目标值", async () => {
    const propertyData: SmilesQueryResponse = {
      ...QUERY_DATA,
      match_type: "property",
      predicted_property_name: "Melting temperature",
      predicted_property_value: 212.5,
      predicted_property_unit: "°C",
      results: QUERY_DATA.results.map((result) => ({
        ...result,
        matched_property_name: "Melting temperature",
        matched_property_value: 208.2,
        matched_property_unit: "°C",
        matched_property_source: "experimental"
      }))
    };
    const { submitQuery } = renderPage({ queryData: propertyData });
    const panel = openParameters();
    fireEvent.click(within(panel).getByRole("radio", { name: /性能相似/ }));
    fireEvent.click(within(panel).getByRole("radio", { name: /熔融温度/ }));
    fireEvent.click(within(panel).getByRole("button", { name: "运行探索" }));

    await waitFor(() => expect(submitQuery).toHaveBeenCalledWith(expect.objectContaining({
      match_mode: "property",
      property_name: "Melting temperature"
    })));
    expect(screen.getByText(/212\.500/)).toBeTruthy();
    expect(screen.getByText(/208\.200/)).toBeTruthy();
    expect(screen.getByText("实验")).toBeTruthy();
  });

  it("长提交结构默认折叠，并可展开查看完整 SMILES", async () => {
    const longSmiles = "*c1ccc(C(=O)Oc2ccc(-c3ccc(OCC(=O)c4ccc(N5CC(=O)c6ccc(OC(=O)c7ccc(C(C)c8ccc(C(=O)Oc9ccc%10c(c9)C(=O)N(*)C%10=O)o8)o7)cc6C5=O)cc4)o3)o2)cc1";
    mocks.resolveSmilesForSearch.mockResolvedValue(longSmiles);
    const view = renderPage({ structure: makeStructure(longSmiles) });
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行探索" }));
    await screen.findByText("示例聚合物");

    const summary = view.container.querySelector(".np-se-result-summary");
    const cards = summary?.querySelectorAll(":scope > div");
    expect(cards?.[0].classList.contains("np-se-result-summary__structure")).toBe(true);
    expect(cards?.[1].textContent).toContain("查询耗时");
    expect(cards?.[2].textContent).toContain("探索口径");

    const smiles = summary?.querySelector("code");
    expect(smiles?.classList.contains("is-collapsed")).toBe(true);
    const expand = screen.getByRole("button", { name: "展开完整提交结构" });
    expect(expand.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(expand);
    expect(screen.getByRole("button", { name: "收起完整提交结构" }).getAttribute("aria-expanded")).toBe("true");
    expect(smiles?.classList.contains("is-collapsed")).toBe(false);
  });

  it("空画板不提交并保持参数浮层打开", async () => {
    mocks.resolveSmilesForSearch.mockResolvedValue("");
    const { submitQuery } = renderPage({ structure: makeStructure("") });
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行探索" }));

    await waitFor(() => expect(mocks.resolveSmilesForSearch).toHaveBeenCalledOnce());
    expect(submitQuery).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "相似性探索参数" })).toBeTruthy();
  });

  it("画板同步期间立即打开准备状态，并阻止重复运行", async () => {
    let resolveSmiles: ((value: string) => void) | undefined;
    let resolveQuery: (() => void) | undefined;
    mocks.resolveSmilesForSearch.mockReturnValue(new Promise<string>((resolve) => {
      resolveSmiles = resolve;
    }));
    const submitQuery = vi.fn(() => new Promise<void>((resolve) => {
      resolveQuery = resolve;
    }));
    renderPage({ submitQuery });
    const panel = openParameters();
    const submit = within(panel).getByRole("button", { name: "运行探索" });
    fireEvent.click(submit);

    expect(await screen.findByRole("dialog", { name: "相似性探索结果" })).toBeTruthy();
    expect(screen.getAllByText("正在准备检索结构")).toHaveLength(2);
    fireEvent.submit(panel.querySelector("form") as HTMLFormElement);
    expect(mocks.resolveSmilesForSearch).toHaveBeenCalledOnce();

    resolveSmiles?.("*CC*");
    await waitFor(() => expect(submitQuery).toHaveBeenCalledOnce());
    await waitFor(() => expect(screen.queryAllByText("正在准备检索结构")).toHaveLength(0));
    resolveQuery?.();
  });

  it("关闭结果后提供侧边展开把手，重开不重复检索", async () => {
    const { submitQuery } = renderPage();
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行探索" }));
    await screen.findByText("示例聚合物");
    fireEvent.click(screen.getByRole("button", { name: "关闭相似性探索结果" }));

    const reopen = await screen.findByRole("button", { name: "展开相似性探索结果" });
    expect(reopen.classList.contains("is-side-handle")).toBe(true);
    fireEvent.click(reopen);
    expect(await screen.findByText("示例聚合物")).toBeTruthy();
    expect(submitQuery).toHaveBeenCalledOnce();
  });

  it("结构或参数变化后保留结果并标记提交快照过期", async () => {
    const structure = makeStructure("*CC*");
    const view = renderPage({ structure });
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行探索" }));
    await screen.findByText("示例聚合物");

    view.rerender(
      <PolymerSimilarityExplorerPage
        structure={{ ...structure, smiles: "CCO" }}
        request={DEFAULT_REQUEST}
        setRequest={view.setRequest}
        isQueryLoading={false}
        queryError={null}
        queryData={QUERY_DATA}
        submitQuery={view.submitQuery}
      />
    );
    expect(screen.getByText(/当前结构或探索参数已变化/)).toBeTruthy();
    expect(screen.getByText("*CC*")).toBeTruthy();
  });

  it("首次切换 3D 时才挂载三维预览", async () => {
    renderPage();
    expect(screen.queryByTestId("similarity-structure-3d")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "3D构象" }));
    await waitFor(() => expect(mocks.toggle3D).toHaveBeenCalledOnce());
    expect(await screen.findByTestId("similarity-structure-3d")).toBeTruthy();
  });

  it("加载示例并暴露离页静默同步句柄", async () => {
    const ref = createRef<StructureCanvasOwnerHandle>();
    render(
      <PolymerSimilarityExplorerPage
        ref={ref}
        structure={makeStructure()}
        request={DEFAULT_REQUEST}
        setRequest={vi.fn()}
        isQueryLoading={false}
        queryError={null}
        queryData={null}
        submitQuery={vi.fn().mockResolvedValue(undefined)}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: "加载结构" }));
    await waitFor(() => expect(mocks.loadStructure).toHaveBeenCalledWith("*CC*"));

    await ref.current?.syncBeforeLeave();
    expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledWith({ preserveExisting: true, quiet: true });
  });
});
