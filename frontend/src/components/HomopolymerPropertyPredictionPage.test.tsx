import { StructureWorkspace } from "../structure/workspace";
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

vi.mock("@structure-editor-engine", () => import("../test/structureEditorEngineMock"));

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
  predictSmiles: vi.fn()
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
    syncBeforeLeave: async () => {
      await mocks.syncSmilesFromCanvas({ preserveExisting: true, quiet: true });
      return { status: "saved" as const };
    },
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
    workspace: new StructureWorkspace(smiles),
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

function panel() {
  return screen.getByRole("complementary", { name: "性质预测" });
}

function openSelection() {
  const toggle = within(panel()).queryByRole("button", { name: "调整" });
  if (toggle) fireEvent.click(toggle);
  return panel();
}

function resultsTable() {
  return within(panel()).getAllByRole("table").at(-1)!;
}

function run() {
  fireEvent.click(within(panel()).getByRole("button", { name: /运行预测|重新预测/ }));
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
  mocks.predictSmiles.mockResolvedValue({
    predictions: { "Glass transition temperature": 123.456 },
    query_time_ms: 12.3
  });
});

afterEach(() => cleanup());

describe("HomopolymerPropertyPredictionPage", () => {
  it("结构画板与常驻任务面板并排，运行前展开九项性质选择并列出占位结果", () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);

    expect(screen.getByRole("heading", { name: "均聚物性质预测" })).toBeTruthy();
    expect(screen.getByTitle("均聚物性质预测结构编辑器")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "预测参数" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(within(panel()).getByText("全部 9 项性质")).toBeTruthy();
    expect(within(panel()).getByRole("button", { name: "运行预测" })).toBeTruthy();
    expect(within(panel()).getAllByText("—")).toHaveLength(9);

    const checkboxes = within(panel()).getAllByRole("checkbox") as HTMLInputElement[];
    expect(checkboxes).toHaveLength(9);
    expect(checkboxes.every((checkbox) => checkbox.checked)).toBe(true);
    expect(within(panel()).getByRole("button", { name: "收起" }).getAttribute("aria-expanded")).toBe("true");
  });

  it("首次成功运行后收起性质选择，之后可再展开", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    run();
    await waitFor(() => expect(within(resultsTable()).getByText("123")).toBeTruthy());
    expect(within(panel()).queryAllByRole("checkbox")).toHaveLength(0);
    fireEvent.click(within(panel()).getByRole("button", { name: "调整" }));
    expect(within(panel()).getAllByRole("checkbox")).toHaveLength(9);
  });

  it("支持清空、分组和全选，并以固定顺序提交所选性质", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    const editor = openSelection();

    fireEvent.click(within(editor).getByRole("button", { name: "清空" }));
    expect(within(editor).getByText("未选择性质")).toBeTruthy();
    expect((within(editor).getByRole("button", { name: "运行预测" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(within(editor).getAllByRole("button", { name: "选择本组" })[0]);
    expect(within(editor).getByText("已选 4 / 9 项")).toBeTruthy();
    expect(within(editor).getByText("Tg、Tm、Td、失重率")).toBeTruthy();
    fireEvent.click(within(editor).getByRole("button", { name: "全选" }));
    run();

    await waitFor(() => expect(mocks.predictSmiles).toHaveBeenCalledOnce());
    expect(mocks.predictSmiles).toHaveBeenCalledWith(
      { smiles: "*CC*", properties: [...PREDICTABLE_PROPERTIES] },
      expect.any(AbortSignal)
    );
    expect(screen.queryByRole("button", { name: "全选" })).toBeNull();
  });

  it.each(["CCO", "*CC*"])("兼容结构 %s 且将缺失性质明确标记为未返回", async (smiles) => {
    mocks.resolveSmilesForSearch.mockResolvedValue(smiles);
    render(<HomopolymerPropertyPredictionPage structure={makeStructure(smiles)} />);
    run();

    expect(await screen.findByText("123")).toBeTruthy();
    expect(screen.getAllByText("未返回")).toHaveLength(8);
    expect(screen.getByText("12.3 ms")).toBeTruthy();
    expect(mocks.predictSmiles).toHaveBeenCalledWith(
      expect.objectContaining({ smiles }),
      expect.any(AbortSignal)
    );
  });

  it("空画板不提交并在运行按钮下提示先画结构", async () => {
    mocks.resolveSmilesForSearch.mockResolvedValue("");
    render(<HomopolymerPropertyPredictionPage structure={makeStructure("")} />);
    run();

    await waitFor(() => expect(mocks.resolveSmilesForSearch).toHaveBeenCalledOnce());
    expect(mocks.predictSmiles).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/请先在左侧画出结构/)).toBeTruthy();
  });

  it("显示后端错误，并能就地重试", async () => {
    mocks.predictSmiles.mockRejectedValueOnce(new Error("prediction service is disabled"));
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    run();

    expect(await screen.findByText("prediction service is disabled")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("123")).toBeTruthy();
    expect(mocks.predictSmiles).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("prediction service is disabled")).toBeNull();
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

    run();
    await waitFor(() => expect(signals).toHaveLength(1));
    fireEvent.click(await within(panel()).findByRole("button", { name: "重新预测" }));

    await waitFor(() => expect(signals).toHaveLength(2));
    expect(signals[0].aborted).toBe(true);
    expect(await screen.findByText("222")).toBeTruthy();
    expect(screen.queryByText(/第 2 次运行/)).toBeNull();
  });

  it("第二次运行后自动与上次结果对比，可切换为不对比", async () => {
    mocks.predictSmiles
      .mockResolvedValueOnce({ predictions: { "Glass transition temperature": 100 }, query_time_ms: 5 })
      .mockResolvedValueOnce({ predictions: { "Glass transition temperature": 150 }, query_time_ms: 6 });
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    run();
    expect(await screen.findByText("100")).toBeTruthy();
    expect(screen.queryByRole("columnheader", { name: "变化" })).toBeNull();

    run();
    expect(await screen.findByText(/第 2 次运行/)).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "上次" })).toBeTruthy();
    const delta = within(resultsTable()).getByText("+50");
    expect(delta.classList.contains("is-large")).toBe(true);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "none" } });
    expect(screen.queryByRole("columnheader", { name: "变化" })).toBeNull();
    expect(within(resultsTable()).getByText("150")).toBeTruthy();
  });

  it("结构变化后保留上次结果并显示提交结构", async () => {
    const structure = makeStructure("*CC*");
    const view = render(<HomopolymerPropertyPredictionPage structure={structure} />);
    run();
    await screen.findByText("123");
    expect(screen.queryByText("提交结构")).toBeNull();

    view.rerender(
      <HomopolymerPropertyPredictionPage structure={{ ...structure, smiles: "CCO" }} />
    );
    expect(screen.getByText(/当前结构或性质选择已变化/)).toBeTruthy();
    expect(screen.getByText("*CC*")).toBeTruthy();
  });

  it("性质选择变化后保留提交快照与旧结果", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    run();
    await screen.findByText("123");

    const editor = openSelection();
    fireEvent.click(within(editor).getByRole("checkbox", { name: /玻璃化转变温度/ }));
    fireEvent.click(within(editor).getByRole("button", { name: "收起" }));

    expect(screen.getByText(/当前结构或性质选择已变化/)).toBeTruthy();
    expect(screen.getByText("123")).toBeTruthy();
    expect(screen.getAllByText("未返回")).toHaveLength(8);
  });

  it("导出菜单可复制制表符分隔的结果表", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    run();
    await screen.findByText("123");

    fireEvent.click(screen.getByRole("button", { name: "导出" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "复制表格" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    const text = writeText.mock.calls[0][0] as string;
    expect(text.split("\n")[0]).toBe("property_key\tproperty\tunit\tvalue\tsmiles");
    expect(text).toContain("Glass transition temperature\t玻璃化转变温度\t°C\t123.456\t*CC*");
    expect(await screen.findByText("已复制，可粘贴到表格")).toBeTruthy();
  });

  it("Ctrl+Enter 在页面内运行预测", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    fireEvent.keyDown(screen.getByLabelText("SMILES 输入，自动同步到画板"), { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(mocks.predictSmiles).toHaveBeenCalledOnce());
  });

  it("空画板显示示例，点击示例经由画板写入结构", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure("")} />);
    const examples = screen.getByRole("list", { name: "示例结构" });
    fireEvent.click(within(examples).getByRole("button", { name: /PS/ }));
    await waitFor(() => expect(mocks.loadStructure).toHaveBeenCalledWith("*CC(*)c1ccccc1"));
    expect(mocks.cancelSmilesDraftSync).toHaveBeenCalled();
  });

  it("有结构时不显示示例", () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure("*CC*")} />);
    expect(screen.queryByRole("list", { name: "示例结构" })).toBeNull();
  });

  it("本次会话的运行可恢复较早的结构与结果", async () => {
    mocks.resolveSmilesForSearch.mockResolvedValueOnce("*CC*").mockResolvedValueOnce("CCO");
    mocks.predictSmiles
      .mockResolvedValueOnce({ predictions: { "Glass transition temperature": 100 }, query_time_ms: 5 })
      .mockResolvedValueOnce({ predictions: { "Glass transition temperature": 150 }, query_time_ms: 6 });
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    run();
    await waitFor(() => expect(within(resultsTable()).getByText("100")).toBeTruthy());
    expect(screen.queryByRole("heading", { name: "本次会话的运行" })).toBeNull();
    run();
    expect(await screen.findByRole("heading", { name: "本次会话的运行" })).toBeTruthy();

    const runs = screen.getByRole("heading", { name: "本次会话的运行" }).closest("section")!;
    const [current, earlier] = within(runs).getAllByRole("button");
    expect(current.getAttribute("aria-current")).toBe("true");
    fireEvent.click(earlier);

    await waitFor(() => expect(mocks.loadStructure).toHaveBeenCalledWith("*CC*"));
    expect(within(resultsTable()).getByText("100")).toBeTruthy();
    expect(screen.getByText(/第 1 次运行/)).toBeTruthy();
  });

  it("使用说明在辅助栏打开，Escape 关闭并把焦点还给入口", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    const trigger = screen.getByRole("button", { name: "使用说明" });
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-pressed")).toBe("true");
    const panel = screen.getByRole("tabpanel", { name: "使用说明" });
    expect(within(panel).getByRole("heading", { name: "运行与对比" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "使用说明" }).getAttribute("aria-selected")).toBe("true");

    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(screen.queryByRole("tabpanel", { name: "使用说明" })).not.toBeNull();
    const tab = screen.getByRole("tab", { name: "使用说明" });
    tab.focus();
    fireEvent.keyDown(tab, { key: "Escape" });
    expect(screen.queryByRole("tabpanel", { name: "使用说明" })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("AI 助手在辅助栏打开，与使用说明共用一个辅助栏", () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    const ai = screen.getByRole("button", { name: "AI 助手" });
    const help = screen.getByRole("button", { name: "使用说明" });

    fireEvent.click(ai);
    expect(ai.getAttribute("aria-pressed")).toBe("true");
    const panel = screen.getByRole("tabpanel", { name: "AI 助手" });
    expect(within(panel).getByText(/界面预览：AI 助手尚未接入/)).toBeTruthy();
    expect(within(panel).getByRole("textbox", { name: "发送给 AI 助手的消息" })).toBeTruthy();

    fireEvent.click(help);
    expect(ai.getAttribute("aria-pressed")).toBe("false");
    expect(help.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("tabpanel", { name: "使用说明" })).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: "AI 助手" }));
    expect(ai.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(ai);
    expect(screen.queryByRole("tabpanel")).toBeNull();
  });

  it("AI 助手界面预览：建议问题、附带上下文与清空对话都只在本页完成", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    run();
    await waitFor(() => expect(within(resultsTable()).getByText("123")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "AI 助手" }));
    const panel = screen.getByRole("tabpanel", { name: "AI 助手" });

    expect(within(panel).getByLabelText("AI 可读取的页面内容").textContent).toContain("1 项预测结果");
    fireEvent.click(within(panel).getByLabelText("附带当前结构和结果"));
    fireEvent.click(within(panel).getByRole("button", { name: "解释这次的预测结果" }));

    const log = within(panel).getByRole("log", { name: "对话" });
    expect(within(log).getByText("解释这次的预测结果")).toBeTruthy();
    expect(within(log).getByText(/结合当前结构和第 1 次运行的 1 项预测结果/)).toBeTruthy();
    expect(mocks.predictSmiles).toHaveBeenCalledOnce();

    const input = within(panel).getByRole("textbox", { name: "发送给 AI 助手的消息" });
    fireEvent.change(input, { target: { value: "Tm 为什么这么高？" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(within(log).getByText("Tm 为什么这么高？")).toBeTruthy();
    expect((input as HTMLTextAreaElement).value).toBe("");

    fireEvent.click(within(panel).getByRole("button", { name: "清空对话" }));
    expect(within(log).queryByText("Tm 为什么这么高？")).toBeNull();
    expect(within(panel).getByRole("button", { name: "解释这次的预测结果" })).toBeTruthy();
  });

  it("没有结构时不显示“用这个结构继续”", () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure("")} onOpenModule={vi.fn()} />);
    expect(screen.queryByRole("navigation", { name: "用这个结构继续" })).toBeNull();
  });

  it("用这个结构继续跳转到相关模块", () => {
    const onOpenModule = vi.fn();
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} onOpenModule={onOpenModule} />);
    const relay = screen.getByRole("navigation", { name: "用这个结构继续" });
    fireEvent.click(within(relay).getByRole("button", { name: "相似性探索" }));
    expect(onOpenModule).toHaveBeenCalledWith("explorer");
  });

  it("首次切换 3D 时才挂载三维预览", async () => {
    render(<HomopolymerPropertyPredictionPage structure={makeStructure()} />);
    expect(screen.queryByTestId("prediction-structure-3d")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "3D构象" }));
    await waitFor(() => expect(mocks.toggle3D).toHaveBeenCalledOnce());
    expect(await screen.findByTestId("prediction-structure-3d")).toBeTruthy();
  });

  it("加载 *CC* 示例并暴露离页静默同步句柄", async () => {
    const ref = createRef<StructureCanvasOwnerHandle>();
    render(<HomopolymerPropertyPredictionPage ref={ref} structure={makeStructure()} />);
    fireEvent.click(screen.getByRole("button", { name: "加载示例" }));
    await waitFor(() => expect(mocks.loadStructure).toHaveBeenCalledWith("*CC*"));

    await ref.current?.syncBeforeLeave();
    expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledWith({ preserveExisting: true, quiet: true });
  });
});
