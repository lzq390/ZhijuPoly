// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import type { StructureWorkspaceContext } from "../types";
import {
  StructureWorkbenchPage,
  type StructureWorkbenchHandle
} from "./StructureWorkbenchPage";

const mocks = vi.hoisted(() => ({
  loadStructure: vi.fn(),
  applyTextStructure: vi.fn(),
  clearCanvas: vi.fn(),
  importImageFile: vi.fn(),
  syncSmilesFromCanvas: vi.fn(),
  toggle3D: vi.fn(),
  copySmiles: vi.fn(),
  handleEditorLoad: vi.fn(),
  setFeedback: vi.fn(),
  predictMonomerPrecursors: vi.fn(),
  fetchStructure2D: vi.fn()
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
    setFeedback: mocks.setFeedback,
    copyState: "idle",
    loadStructure: mocks.loadStructure,
    applyTextStructure: mocks.applyTextStructure,
    clearCanvas: mocks.clearCanvas,
    importImageFile: mocks.importImageFile,
    syncSmilesFromCanvas: mocks.syncSmilesFromCanvas,
    toggle3D: mocks.toggle3D,
    copySmiles: mocks.copySmiles
  })
}));

vi.mock("../services/api", () => ({
  predictMonomerPrecursors: mocks.predictMonomerPrecursors,
  fetchStructure2D: mocks.fetchStructure2D
}));

vi.mock("./StructurePreview3D", () => ({
  StructurePreview3D: ({ smiles }: { smiles: string }) => (
    <div data-testid="structure-preview-3d">{smiles}</div>
  )
}));

let mobileMatches = false;

function installMatchMedia() {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: mobileMatches,
      media: "(max-width: 767px)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    }))
  });
}

function makeStructure(smiles = "*CC*"): StructureWorkspaceContext {
  return {
    smiles,
    setSmiles: vi.fn(),
    iframeRef: { current: null },
    setIsReady: vi.fn(),
    getCurrentSmiles: vi.fn().mockResolvedValue(smiles)
  };
}

function renderPage(structure = makeStructure(), onOpenModule = vi.fn()) {
  return {
    structure,
    onOpenModule,
    ...render(<StructureWorkbenchPage structure={structure} onOpenModule={onOpenModule} />)
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mobileMatches = false;
  installMatchMedia();
  mocks.loadStructure.mockResolvedValue(true);
  mocks.applyTextStructure.mockImplementation(async (value: string) => ({
    applied: true,
    smiles: value.trim()
  }));
  mocks.clearCanvas.mockResolvedValue(true);
  mocks.importImageFile.mockResolvedValue(true);
  mocks.syncSmilesFromCanvas.mockResolvedValue("*CC*");
  mocks.toggle3D.mockResolvedValue(true);
  mocks.fetchStructure2D.mockResolvedValue({ structure_svg: "<svg />" });
});

afterEach(() => cleanup());

describe("StructureWorkbenchPage", () => {
  it("使用独立工作台结构，并保留完整工具行为", async () => {
    const view = renderPage();

    expect(view.container.querySelector(".np-structure-workbench")).toBeTruthy();
    expect(view.container.querySelector(".tg-reverse-page")).toBeNull();
    expect(screen.getByRole("heading", { name: "结构工作台" })).toBeTruthy();
    expect(screen.queryByTestId("structure-preview-3d")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "加载示例" }));
    await waitFor(() => expect(mocks.loadStructure).toHaveBeenCalledWith(REVERSE_DESIGN_DEMO_SMILES));

    fireEvent.load(screen.getByTitle("结构工作台结构编辑器"));
    expect(mocks.handleEditorLoad).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "3D 构象" }));
    await waitFor(() => expect(mocks.toggle3D).toHaveBeenCalledOnce());
    expect(screen.getByTestId("structure-preview-3d").textContent).toBe("*CC*");
  });

  it("SMILES 输入只修改草稿，显式应用后才调用画布能力", async () => {
    const { structure } = renderPage();
    const input = screen.getByLabelText("结构 SMILES 草稿");

    fireEvent.change(input, { target: { value: "  *CO*  " } });
    expect(structure.setSmiles).not.toHaveBeenCalled();
    expect(screen.getByText("有未应用修改")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "应用结构" }));
    await waitFor(() => expect(mocks.applyTextStructure).toHaveBeenCalledWith("*CO*"));
    expect(screen.getByText("与共享结构同步")).toBeTruthy();
  });

  it("标准化失败只显示内联错误，不覆盖共享结构", async () => {
    mocks.applyTextStructure.mockRejectedValueOnce(new Error("SMILES 无法解析"));
    const { structure } = renderPage();
    fireEvent.change(screen.getByLabelText("结构 SMILES 草稿"), { target: { value: "invalid(" } });
    fireEvent.click(screen.getByRole("button", { name: "应用结构" }));

    expect(await screen.findByText("SMILES 无法解析")).toBeTruthy();
    expect(structure.setSmiles).not.toHaveBeenCalled();
    expect((screen.getByLabelText("结构 SMILES 草稿") as HTMLTextAreaElement).value).toBe("invalid(");
  });

  it("有未应用草稿时保留输入，并可显式采用最新共享结构", async () => {
    const structure = makeStructure("CC");
    const view = render(<StructureWorkbenchPage structure={structure} onOpenModule={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("结构 SMILES 草稿"), { target: { value: "draft-value" } });

    view.rerender(
      <StructureWorkbenchPage structure={{ ...structure, smiles: "NN" }} onOpenModule={vi.fn()} />
    );
    expect((screen.getByLabelText("结构 SMILES 草稿") as HTMLTextAreaElement).value).toBe("draft-value");
    const adoptLatest = await screen.findByRole("button", { name: "使用最新共享结构" });
    fireEvent.click(adoptLatest);
    expect((screen.getByLabelText("结构 SMILES 草稿") as HTMLTextAreaElement).value).toBe("NN");
  });

  it("外部功能先同步一次再导航，内置反推不导航", async () => {
    const onOpenModule = vi.fn();
    renderPage(makeStructure(), onOpenModule);

    fireEvent.click(screen.getByRole("button", { name: "功能参数" }));
    fireEvent.click(screen.getByRole("button", { name: "打开数据库查询" }));
    await waitFor(() => expect(onOpenModule).toHaveBeenCalledWith("databaseQuery"));
    expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "功能参数" }));
    fireEvent.click(screen.getByRole("button", { name: "设置单体逆合成反推参数" }));
    expect(screen.getByLabelText("目标单体 SMILES")).toBeTruthy();
    expect(onOpenModule).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["databaseQuery", "打开数据库查询"],
    ["explorer", "打开聚合物性能探索"],
    ["monomerDft", "打开单体 DFT（AIMNet2）"],
    ["monomerPolymerization", "打开单体正向聚合"],
    ["reverseDesign", "打开Tg 逆向设计"],
    ["conditionalGeneration", "打开条件聚合物生成"],
    ["polytaoGeneration", "打开聚合物生成"]
  ] as const)("模块 %s 保持同步一次、导航一次", async (moduleId, label) => {
    const onOpenModule = vi.fn();
    renderPage(makeStructure(), onOpenModule);
    fireEvent.click(screen.getByRole("button", { name: "功能参数" }));
    fireEvent.click(screen.getByRole("button", { name: label }));

    await waitFor(() => expect(onOpenModule).toHaveBeenCalledWith(moduleId));
    expect(onOpenModule).toHaveBeenCalledTimes(1);
    expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledTimes(1);
  });

  it("AI 助手保留占位交互且明确不发送", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "AI 助手" }));
    fireEvent.click(screen.getByRole("button", { name: "解释当前结构中的主要官能团" }));
    expect((screen.getByLabelText("发送给 AI 助手的消息") as HTMLTextAreaElement).value).toBe("解释当前结构中的主要官能团");
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    expect(screen.getByText("AI 对话接口尚未接入，本次内容未发送。")).toBeTruthy();
    expect(mocks.predictMonomerPrecursors).not.toHaveBeenCalled();
  });

  it("手机默认不挂载 Ketcher，展开后折叠不会重建 iframe", async () => {
    mobileMatches = true;
    installMatchMedia();
    renderPage();

    expect(screen.queryByTitle("结构工作台结构编辑器")).toBeNull();
    expect(screen.getByText("文本优先模式")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开绘图画布" }));
    const iframe = await screen.findByTitle("结构工作台结构编辑器");

    fireEvent.click(screen.getByRole("button", { name: "收起画布" }));
    expect(screen.getByTitle("结构工作台结构编辑器")).toBe(iframe);
    fireEvent.click(screen.getByRole("button", { name: "展开画布" }));
    expect(screen.getByTitle("结构工作台结构编辑器")).toBe(iframe);
  });

  it("反推保持固定 payload，并把 AbortSignal 传给请求", async () => {
    mocks.predictMonomerPrecursors.mockResolvedValue({
      input_smiles: "CC",
      canonical_smiles: "CC",
      target_role: "auto",
      inferred_target_role: "other",
      query_time_ms: 12,
      total: 0,
      candidates: []
    });
    renderPage();

    fireEvent.click(screen.getByRole("button", { name: "功能参数" }));
    fireEvent.click(screen.getByRole("button", { name: "设置单体逆合成反推参数" }));
    fireEvent.change(screen.getByLabelText("反推候选数"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "运行反推" }));

    await waitFor(() => expect(mocks.predictMonomerPrecursors).toHaveBeenCalledOnce());
    expect(mocks.predictMonomerPrecursors).toHaveBeenCalledWith(
      {
        smiles: "C=C(C)C(=O)OC",
        target_role: "auto",
        num_beams: 5,
        num_return_sequences: 3,
        max_new_tokens: 128
      },
      expect.any(AbortSignal)
    );
    expect(await screen.findByText("未找到可展示候选")).toBeTruthy();
  });

  it("重新运行反推会取消旧请求，卸载也会取消当前请求", async () => {
    const signals: AbortSignal[] = [];
    mocks.predictMonomerPrecursors.mockImplementation((_payload, signal: AbortSignal) => {
      signals.push(signal);
      return new Promise(() => undefined);
    });
    const view = renderPage();

    fireEvent.click(screen.getByRole("button", { name: "功能参数" }));
    fireEvent.click(screen.getByRole("button", { name: "设置单体逆合成反推参数" }));
    fireEvent.click(screen.getByRole("button", { name: "运行反推" }));
    await waitFor(() => expect(signals).toHaveLength(1));

    fireEvent.click(screen.getByRole("button", { name: "关闭单体反推结果" }));
    fireEvent.click(screen.getByRole("button", { name: "功能参数" }));
    fireEvent.click(screen.getByRole("button", { name: "运行反推" }));
    await waitFor(() => expect(signals).toHaveLength(2));
    expect(signals[0].aborted).toBe(true);

    view.unmount();
    expect(signals[1].aborted).toBe(true);
  });

  it("暴露导航前静默同步接口", async () => {
    const ref = createRef<StructureWorkbenchHandle>();
    render(<StructureWorkbenchPage ref={ref} structure={makeStructure()} onOpenModule={vi.fn()} />);
    await ref.current?.syncBeforeLeave();
    expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledWith({ preserveExisting: true, quiet: true });
  });

  it("Escape 关闭浮层并恢复触发按钮焦点", async () => {
    renderPage();
    const trigger = screen.getByRole("button", { name: "功能参数" });
    fireEvent.click(trigger);
    expect(screen.getByRole("dialog", { name: "选择功能" })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(screen.queryByRole("dialog", { name: "选择功能" })).toBeNull();
  });
});
