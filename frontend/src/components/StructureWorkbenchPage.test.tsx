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
  canvasState: { isFlipped: false },
  loadStructure: vi.fn(),
  clearCanvas: vi.fn(),
  importImageFile: vi.fn(),
  syncSmilesFromCanvas: vi.fn(),
  toggle3D: vi.fn(),
  copySmiles: vi.fn(),
  handleEditorLoad: vi.fn(),
  setFeedback: vi.fn(),
  predictMonomerPrecursors: vi.fn()
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
    setFeedback: mocks.setFeedback,
    copyState: "idle",
    loadStructure: mocks.loadStructure,
    clearCanvas: mocks.clearCanvas,
    importImageFile: mocks.importImageFile,
    syncSmilesFromCanvas: mocks.syncSmilesFromCanvas,
    toggle3D: mocks.toggle3D,
    copySmiles: mocks.copySmiles
  })
}));

vi.mock("../services/api", () => ({
  predictMonomerPrecursors: mocks.predictMonomerPrecursors
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
  mocks.canvasState.isFlipped = false;
  mobileMatches = false;
  installMatchMedia();
  mocks.loadStructure.mockResolvedValue(true);
  mocks.clearCanvas.mockResolvedValue(true);
  mocks.importImageFile.mockResolvedValue(true);
  mocks.syncSmilesFromCanvas.mockResolvedValue("*CC*");
  mocks.toggle3D.mockImplementation(async () => {
    mocks.canvasState.isFlipped = true;
    return true;
  });
});

afterEach(() => cleanup());

describe("StructureWorkbenchPage", () => {
  it("使用独立工作台结构，并保留完整工具行为", async () => {
    const view = renderPage();

    expect(view.container.querySelector(".np-structure-workbench")).toBeTruthy();
    expect(view.container.querySelector(".tg-reverse-page")).toBeNull();
    expect(screen.getByRole("heading", { name: "结构工作台" })).toBeTruthy();
    expect(screen.queryByTestId("structure-preview-3d")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "加载结构" }));
    await waitFor(() => expect(mocks.loadStructure).toHaveBeenCalledWith(REVERSE_DESIGN_DEMO_SMILES));

    fireEvent.load(screen.getByTitle("结构工作台结构编辑器"));
    expect(mocks.handleEditorLoad).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "3D构象" }));
    await waitFor(() => expect(mocks.toggle3D).toHaveBeenCalledOnce());
    expect(screen.getByTestId("structure-preview-3d").textContent).toBe("*CC*");
    expect(view.container.querySelector(".np-sw-editor")?.classList.contains("is-flipped")).toBe(true);
    expect(screen.getByRole("button", { name: "2D画布" }).getAttribute("aria-pressed")).toBe("true");
    expect(view.container.querySelector(".np-sw-editor__layer--2d")?.hasAttribute("inert")).toBe(true);
    expect(view.container.querySelector(".np-sw-editor__layer--3d")?.hasAttribute("inert")).toBe(false);
  });

  it("严格保留原版只读 SMILES 胶囊和复制行为", () => {
    renderPage();
    const output = screen.getByLabelText("当前共享 SMILES，只读") as HTMLTextAreaElement;

    expect(output.readOnly).toBe(true);
    expect(output.value).toBe("*CC*");
    fireEvent.click(screen.getByRole("button", { name: "复制共享 SMILES" }));
    expect(mocks.copySmiles).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "应用结构" })).toBeNull();
  });

  it("共享结构变化直接反映到原版只读胶囊", () => {
    const structure = makeStructure("CC");
    const view = render(<StructureWorkbenchPage structure={structure} onOpenModule={vi.fn()} />);
    expect((screen.getByLabelText("当前共享 SMILES，只读") as HTMLTextAreaElement).value).toBe("CC");

    view.rerender(
      <StructureWorkbenchPage structure={{ ...structure, smiles: "NN" }} onOpenModule={vi.fn()} />
    );
    expect((screen.getByLabelText("当前共享 SMILES，只读") as HTMLTextAreaElement).value).toBe("NN");
  });

  it("默认页面不再展示原版没有的说明和状态组件", () => {
    renderPage();

    expect(screen.queryByText("统一管理结构输入、二维编辑、三维预览与下游科研任务")).toBeNull();
    expect(screen.queryByLabelText("工作台状态")).toBeNull();
    expect(screen.queryByText("文本优先模式")).toBeNull();
    expect(screen.getByRole("button", { name: "清空画布" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "生成SMILES" }).classList.contains("is-primary")).toBe(true);
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

  it("手机与桌面一样直接挂载原版 Ketcher 工作区", () => {
    mobileMatches = true;
    installMatchMedia();
    renderPage();

    expect(screen.getByTitle("结构工作台结构编辑器")).toBeTruthy();
    expect(screen.queryByText("文本优先模式")).toBeNull();
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
