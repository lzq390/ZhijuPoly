// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PREDICTABLE_PROPERTIES } from "../constants/predictableProperties";
import type { StructureWorkspaceContext } from "../types";
import {
  HomopolymerPropertyPredictionPage
} from "./HomopolymerPropertyPredictionPage";
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
  handleEditorLoad: vi.fn(),
  predictSmiles: vi.fn()
}));

vi.mock("../hooks/useTgStructureCanvas", () => ({
  useTgStructureCanvas: () => ({
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
  predictSmiles: mocks.predictSmiles
}));

vi.mock("./StructurePreview3D", () => ({
  StructurePreview3D: ({ smiles }: { smiles: string }) => (
    <div data-testid="prediction-structure-3d">{smiles}</div>
  )
}));

function makeStructure(smiles = "*CC*"): StructureWorkspaceContext {
  return {
    smiles,
    setSmiles: vi.fn(),
    iframeRef: { current: null },
    setIsReady: vi.fn(),
    getCurrentSmiles: vi.fn().mockResolvedValue(smiles)
  };
}

function abortablePending(signal: AbortSignal) {
  return new Promise<never>((_resolve, reject) => {
    signal.addEventListener(
      "abort",
      () => reject(new DOMException("aborted", "AbortError")),
      { once: true }
    );
  });
}

function openParameters() {
  fireEvent.click(screen.getByRole("button", { name: "预测参数" }));
  return screen.getByRole("dialog", { name: "预测参数" });
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
  mocks.predictSmiles.mockResolvedValue({
    predictions: { "Glass transition temperature": 123.456 },
    query_time_ms: 12.3
  });
});

afterEach(() => cleanup());

describe("HomopolymerPropertyPredictionPage", () => {
  it("复用完整结构画板并默认全选九项性质", () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);

    expect(screen.getByRole("heading", { name: "均聚物性质预测" })).toBeTruthy();
    expect(screen.getByTitle("均聚物性质预测结构编辑器")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "AI 助手" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "性质预测结果" })).toBeNull();
    expect(screen.queryByRole("button", { name: "展开预测结果" })).toBeNull();

    const panel = openParameters();
    const checkboxes = within(panel).getAllByRole("checkbox") as HTMLInputElement[];
    expect(checkboxes).toHaveLength(9);
    expect(checkboxes.every((checkbox) => checkbox.checked)).toBe(true);
    expect(within(panel).getByText("已选 9 / 9 项")).toBeTruthy();
  });

  it("支持清空、分组和全选，并以固定顺序提交所选性质", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    const panel = openParameters();

    fireEvent.click(within(panel).getByRole("button", { name: "清空" }));
    expect(within(panel).getByText("已选 0 / 9 项")).toBeTruthy();
    expect((within(panel).getByRole("button", { name: "运行预测" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(within(panel).getAllByRole("button", { name: "选择本组" })[0]);
    expect(within(panel).getByText("已选 4 / 9 项")).toBeTruthy();
    fireEvent.click(within(panel).getByRole("button", { name: "全选" }));
    fireEvent.click(within(panel).getByRole("button", { name: "运行预测" }));

    await waitFor(() => expect(mocks.predictSmiles).toHaveBeenCalledOnce());
    expect(mocks.predictSmiles).toHaveBeenCalledWith(
      { smiles: "*CC*", properties: [...PREDICTABLE_PROPERTIES] },
      expect.any(AbortSignal)
    );
  });

  it.each(["CCO", "*CC*"])("兼容结构 %s 且将缺失性质明确标记为未返回", async (smiles) => {
    mocks.resolveSmilesForSearch.mockResolvedValue(smiles);
    render(<HomopolymerPropertyPredictionPage structure={makeStructure(smiles)} />);
    const panel = openParameters();
    fireEvent.click(within(panel).getByRole("button", { name: "运行预测" }));

    expect(await screen.findByText("123.46")).toBeTruthy();
    expect(screen.getAllByText("未返回")).toHaveLength(8);
    expect(screen.getByText("12.3 ms")).toBeTruthy();
    expect(mocks.predictSmiles).toHaveBeenCalledWith(
      expect.objectContaining({ smiles }),
      expect.any(AbortSignal)
    );
  });

  it("空画板不提交并保持参数浮层打开", async () => {
    mocks.resolveSmilesForSearch.mockResolvedValue("");
    render(<HomopolymerPropertyPredictionPage structure={makeStructure("")} />);
    const panel = openParameters();
    fireEvent.click(within(panel).getByRole("button", { name: "运行预测" }));

    await waitFor(() => expect(mocks.resolveSmilesForSearch).toHaveBeenCalledOnce());
    expect(mocks.predictSmiles).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "预测参数" })).toBeTruthy();
  });

  it("显示后端错误，关闭后可重开，并能从结果返回参数", async () => {
    mocks.predictSmiles.mockRejectedValue(new Error("prediction service is disabled"));
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行预测" }));

    expect(await screen.findByText("prediction service is disabled")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "关闭性质预测结果" }));
    const reopen = await screen.findByRole("button", { name: "展开预测结果" });
    fireEvent.click(reopen);
    expect(screen.getByRole("dialog", { name: "性质预测结果" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "检查预测参数" }));
    expect(screen.getByRole("dialog", { name: "预测参数" })).toBeTruthy();
  });

  it("关闭成功结果后显示右侧展开把手，重开不会再次运行预测", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行预测" }));
    expect(await screen.findByText("123.46")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "关闭性质预测结果" }));
    const reopen = await screen.findByRole("button", { name: "展开预测结果" });
    expect(reopen.classList.contains("is-side-handle")).toBe(true);
    expect(mocks.predictSmiles).toHaveBeenCalledOnce();

    fireEvent.click(reopen);
    expect(await screen.findByText("123.46")).toBeTruthy();
    expect(mocks.predictSmiles).toHaveBeenCalledOnce();
  });

  it("加载中允许重新提交并取消旧请求", async () => {
    const signals: AbortSignal[] = [];
    mocks.predictSmiles
      .mockImplementationOnce((_payload, signal: AbortSignal) => {
        signals.push(signal);
        return abortablePending(signal);
      })
      .mockImplementationOnce((_payload, signal: AbortSignal) => {
        signals.push(signal);
        return Promise.resolve({
          predictions: { "Glass transition temperature": 222 },
          query_time_ms: 2
        });
      });
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);

    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行预测" }));
    await waitFor(() => expect(signals).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "关闭性质预测结果" }));
    const panel = openParameters();
    fireEvent.click(within(panel).getByRole("button", { name: "重新预测" }));

    await waitFor(() => expect(signals).toHaveLength(2));
    expect(signals[0].aborted).toBe(true);
    expect(await screen.findByText("222.00")).toBeTruthy();
  });

  it("结构变化后保留上次结果并显示中性快照提示", async () => {
    const structure = makeStructure("*CC*");
    const view = render(<HomopolymerPropertyPredictionPage structure={structure} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行预测" }));
    await screen.findByText("123.46");

    view.rerender(
      <HomopolymerPropertyPredictionPage structure={{ ...structure, smiles: "CCO" }} />
    );
    expect(screen.getByText(/当前结构或性质选择已变化/)).toBeTruthy();
    expect(screen.getByText("*CC*")).toBeTruthy();
  });

  it("性质选择变化后保留提交快照与旧结果", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    fireEvent.click(within(openParameters()).getByRole("button", { name: "运行预测" }));
    await screen.findByText("123.46");

    fireEvent.click(screen.getByRole("button", { name: "关闭性质预测结果" }));
    const panel = openParameters();
    fireEvent.click(within(panel).getByRole("checkbox", { name: /玻璃化转变温度/ }));
    fireEvent.click(within(panel).getByRole("button", { name: "收起预测参数" }));
    fireEvent.click(screen.getByRole("button", { name: "展开预测结果" }));

    expect(screen.getByText(/当前结构或性质选择已变化/)).toBeTruthy();
    expect(screen.getByText("123.46")).toBeTruthy();
    expect(screen.getAllByText("未返回")).toHaveLength(8);
  });

  it("首次切换 3D 时才挂载三维预览", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    expect(screen.queryByTestId("prediction-structure-3d")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "3D构象" }));
    await waitFor(() => expect(mocks.toggle3D).toHaveBeenCalledOnce());
    expect(await screen.findByTestId("prediction-structure-3d")).toBeTruthy();
  });

  it("参数浮层支持 Escape 和焦点恢复", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    const trigger = screen.getByRole("button", { name: "预测参数" });
    fireEvent.click(trigger);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(screen.queryByRole("dialog", { name: "预测参数" })).toBeNull();
  });

  it("加载 *CC* 示例并暴露离页静默同步句柄", async () => {
    const ref = createRef<StructureCanvasOwnerHandle>();
    render(<HomopolymerPropertyPredictionPage ref={ref} structure={makeStructure()} />);
    fireEvent.click(screen.getByRole("button", { name: "加载结构" }));
    await waitFor(() => expect(mocks.loadStructure).toHaveBeenCalledWith("*CC*"));

    await ref.current?.syncBeforeLeave();
    expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledWith({ preserveExisting: true, quiet: true });
  });
});
