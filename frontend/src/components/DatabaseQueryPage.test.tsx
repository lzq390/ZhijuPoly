// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SmilesLookupResponse, StructureWorkspaceContext } from "../types";
import { DatabaseQueryPage } from "./DatabaseQueryPage";
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
  handleEditorLoad: vi.fn(),
  setFeedback: vi.fn(),
  lookupSmilesInDatabase: vi.fn()
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
    setFeedback: mocks.setFeedback,
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

vi.mock("../services/api", () => ({
  lookupSmilesInDatabase: mocks.lookupSmilesInDatabase
}));

vi.mock("./StructurePreview3D", () => ({
  StructurePreview3D: ({ smiles }: { smiles: string }) => (
    <div data-testid="database-query-structure-3d">{smiles}</div>
  )
}));

const LOOKUP_DATA: SmilesLookupResponse = {
  query_smiles: "*CC*",
  canonical_smiles: "*CC*",
  table: "polymers",
  exists: true,
  total: 1,
  query_time_ms: 8.4,
  results: [
    {
      record_id: "polymer-1",
      source_column: "canonical_smiles",
      smiles: "*CC*",
      canonical_smiles: "*CC*",
      structure_svg: null,
      summary: "聚合物精确匹配",
      fields: { polymer_id: "polymer-1", property_count: 4 }
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

function openParameters() {
  fireEvent.click(screen.getByRole("button", { name: "查询参数" }));
  return screen.getByRole("dialog", { name: "数据库查询参数" });
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
  mocks.lookupSmilesInDatabase.mockResolvedValue(LOOKUP_DATA);
});

afterEach(() => cleanup());

describe("DatabaseQueryPage", () => {
  it("复用结构工作台画板，并在首次查询前隐藏结果抽屉", () => {
    render(<DatabaseQueryPage structure={makeStructure()} />);

    expect(screen.getByRole("heading", { name: "数据库查询" })).toBeTruthy();
    expect(screen.getByTitle("数据库查询结构编辑器")).toBeTruthy();
    expect(screen.getByRole("button", { name: "加载结构" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "AI 助手" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "数据库查询结果" })).toBeNull();
    expect(screen.queryByRole("button", { name: "展开数据库查询结果" })).toBeNull();
  });

  it("默认查询 Polymers，并按所选数据表提交规范化结构", async () => {
    render(<DatabaseQueryPage structure={makeStructure()} />);
    const panel = openParameters();

    expect(within(panel).getByRole("radio", { name: /Polymers/ }).getAttribute("aria-checked")).toBe("true");
    fireEvent.click(within(panel).getByRole("radio", { name: /Properties/ }));
    fireEvent.click(within(panel).getByRole("button", { name: "运行查询" }));

    await waitFor(() => expect(mocks.lookupSmilesInDatabase).toHaveBeenCalledOnce());
    expect(mocks.lookupSmilesInDatabase).toHaveBeenCalledWith(
      { smiles: "*CC*", table: "properties" },
      expect.any(AbortSignal)
    );
    expect(screen.getByRole("dialog", { name: "数据库查询结果" })).toBeTruthy();
  });

  it("画板同步时立即展示准备状态，空结构不请求并重新打开参数", async () => {
    let resolveSmiles: ((value: string) => void) | undefined;
    mocks.resolveSmilesForSearch.mockReturnValue(new Promise<string>((resolve) => {
      resolveSmiles = resolve;
    }));
    render(<DatabaseQueryPage structure={makeStructure("")} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行查询" }));

    expect(await screen.findByRole("dialog", { name: "数据库查询结果" })).toBeTruthy();
    expect(screen.getAllByText("正在准备查询结构")).toHaveLength(2);
    resolveSmiles?.("");

    await waitFor(() => expect(screen.getByRole("dialog", { name: "数据库查询参数" })).toBeTruthy());
    expect(mocks.lookupSmilesInDatabase).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "展开数据库查询结果" })).toBeNull();
  });

  it("成功结果展示提交快照、查询口径和耗时，关闭后可直接重开", async () => {
    render(<DatabaseQueryPage structure={makeStructure()} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行查询" }));

    expect(await screen.findByText("聚合物精确匹配")).toBeTruthy();
    expect(screen.getByText("8.4 ms")).toBeTruthy();
    expect(screen.getByText("Polymers")).toBeTruthy();
    expect(screen.getByText("找到 1 条记录")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "关闭数据库查询结果" }));

    const reopen = await screen.findByRole("button", { name: "展开数据库查询结果" });
    expect(reopen.classList.contains("is-side-handle")).toBe(true);
    fireEvent.click(reopen);
    expect(await screen.findByText("聚合物精确匹配")).toBeTruthy();
    expect(mocks.lookupSmilesInDatabase).toHaveBeenCalledOnce();
  });

  it("长提交结构默认折叠，并可在结果摘要中展开", async () => {
    const longSmiles = "*c1ccc(C(=O)Oc2ccc(-c3ccc(OCC(=O)c4ccc(N5CC(=O)c6ccc(OC(=O)c7ccc(C(C)c8ccc(C(=O)Oc9ccc%10c(c9)C(=O)N(*)C%10=O)o8)o7)cc6C5=O)cc4)o3)o2)cc1";
    mocks.resolveSmilesForSearch.mockResolvedValue(longSmiles);
    render(<DatabaseQueryPage structure={makeStructure(longSmiles)} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行查询" }));
    await screen.findByText("聚合物精确匹配");

    const submitted = screen.getByRole("button", { name: "展开完整提交结构" });
    const contentId = submitted.getAttribute("aria-controls");
    const content = contentId ? document.getElementById(contentId) : null;
    expect(submitted.getAttribute("aria-expanded")).toBe("false");
    expect(content?.classList.contains("is-collapsed")).toBe(true);

    fireEvent.click(submitted);
    expect(screen.getByRole("button", { name: "收起完整提交结构" }).getAttribute("aria-expanded")).toBe("true");
    expect(content?.classList.contains("is-collapsed")).toBe(false);
  });

  it("Properties 结果支持性能名前缀筛选", async () => {
    mocks.lookupSmilesInDatabase.mockResolvedValue({
      ...LOOKUP_DATA,
      table: "properties",
      total: 2,
      results: [
        { ...LOOKUP_DATA.results[0], record_id: "p-1", fields: { property_name: "Dynamic mechanical loss" } },
        { ...LOOKUP_DATA.results[0], record_id: "p-2", fields: { property_name: "Glass transition temperature" } }
      ]
    });
    render(<DatabaseQueryPage structure={makeStructure()} />);
    const panel = openParameters();
    fireEvent.click(within(panel).getByRole("radio", { name: /Properties/ }));
    fireEvent.click(within(panel).getByRole("button", { name: "运行查询" }));

    const filter = await screen.findByRole("searchbox", { name: "筛选性能名称" });
    fireEvent.change(filter, { target: { value: "Dynamic" } });
    expect(screen.getByText("Dynamic mechanical loss")).toBeTruthy();
    expect(screen.queryByText("Glass transition temperature")).toBeNull();
    expect(screen.getByText("显示 1 / 2 条")).toBeTruthy();
  });

  it("结构或数据表变化后保留结果并标记提交快照过期", async () => {
    const structure = makeStructure("*CC*");
    const view = render(<DatabaseQueryPage structure={structure} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行查询" }));
    await screen.findByText("聚合物精确匹配");

    view.rerender(<DatabaseQueryPage structure={{ ...structure, smiles: "CCO" }} />);
    expect(screen.getByText(/当前结构或目标数据表已变化/)).toBeTruthy();
    expect(screen.getByText("聚合物精确匹配")).toBeTruthy();
  });

  it("显示后端错误，并在卸载时取消尚未完成的请求", async () => {
    let signal: AbortSignal | undefined;
    mocks.lookupSmilesInDatabase.mockImplementationOnce((_payload, nextSignal: AbortSignal) => {
      signal = nextSignal;
      return new Promise(() => undefined);
    });
    const view = render(<DatabaseQueryPage structure={makeStructure()} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行查询" }));
    await waitFor(() => expect(signal).toBeTruthy());
    view.unmount();
    expect(signal?.aborted).toBe(true);

    mocks.lookupSmilesInDatabase.mockRejectedValueOnce(new Error("database service unavailable"));
    render(<DatabaseQueryPage structure={makeStructure()} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行查询" }));
    expect(await screen.findByText("database service unavailable")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "检查查询参数" }));
    expect(screen.getByRole("dialog", { name: "数据库查询参数" })).toBeTruthy();
  });

  it("3D 首次切换时才挂载预览，并向应用暴露离页同步", async () => {
    const ownerRef = createRef<StructureCanvasOwnerHandle>();
    const view = render(<DatabaseQueryPage ref={ownerRef} structure={makeStructure()} />);
    expect(screen.queryByTestId("database-query-structure-3d")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "3D构象" }));
    await waitFor(() => expect(mocks.toggle3D).toHaveBeenCalledOnce());
    expect(screen.getByTestId("database-query-structure-3d")).toBeTruthy();

    await ownerRef.current?.syncBeforeLeave();
    expect(mocks.flushSmilesDraft).toHaveBeenCalledOnce();
    expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledWith({ preserveExisting: true, quiet: true });
    expect(view.container.querySelectorAll('iframe[src="/ketcher/index.html"]')).toHaveLength(1);
  });
});
