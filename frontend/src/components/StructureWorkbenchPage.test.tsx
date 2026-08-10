// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { REVERSE_DESIGN_DEMO_SMILES } from "../constants/reverseDesignDefaults";
import type { StructureWorkspaceContext } from "../types";
import { StructureWorkbenchPage } from "./StructureWorkbenchPage";

const mocks = vi.hoisted(() => ({
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

function makeStructure(): StructureWorkspaceContext {
  return {
    smiles: "*CC*",
    setSmiles: vi.fn(),
    iframeRef: { current: null },
    setIsReady: vi.fn(),
    getCurrentSmiles: vi.fn().mockResolvedValue("*CC*")
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.syncSmilesFromCanvas.mockResolvedValue("*CC*");
});

afterEach(() => {
  cleanup();
});

describe("StructureWorkbenchPage", () => {
  it("复用 Tg 工作区布局，并将加载结构放在导入图片之前", () => {
    const view = render(
      <StructureWorkbenchPage
        structure={makeStructure()}
        onBackHome={vi.fn()}
        onOpenModule={vi.fn()}
      />
    );
    const title = screen.getByRole("heading", { name: "结构工作台" });
    const toolbarButtons = Array.from(
      view.container.querySelectorAll<HTMLButtonElement>(".tg-toolbar > button")
    );

    expect(title.parentElement).toBe(view.container.firstElementChild);
    expect(toolbarButtons.map((button) => button.textContent?.trim())).toEqual([
      "加载结构",
      "导入图片",
      "清空画布",
      "生成SMILES",
      "3D构象",
      "",
      ""
    ]);
    expect(
      toolbarButtons.map((button) => button.dataset.workbenchTool)
    ).toEqual(["load", "import", "clear", "sync", "3d", "modules", "assistant"]);
    expect(
      toolbarButtons.every((button) => button.classList.contains("sw-toolbar-action"))
    ).toBe(true);
    expect(view.container.querySelector(".tg-structure-surface")).toBeTruthy();
    expect(view.container.querySelector(".tg-smiles-capsule")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "加载结构" }));
    expect(mocks.loadStructure).toHaveBeenCalledWith(REVERSE_DESIGN_DEMO_SMILES);

    fireEvent.load(screen.getByTitle("结构工作台结构编辑器"));
    expect(mocks.handleEditorLoad).toHaveBeenCalledOnce();
  });

  it("外部功能单击后直接导航，单体反推才进入参数页", async () => {
    const onOpenModule = vi.fn();
    render(
      <StructureWorkbenchPage
        structure={makeStructure()}
        onBackHome={vi.fn()}
        onOpenModule={onOpenModule}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "功能参数" }));
    expect(screen.getByRole("button", { name: "打开数据库查询" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "设置单体逆合成反推参数" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "打开数据库查询" }));
    await waitFor(() => {
      expect(onOpenModule).toHaveBeenCalledWith("databaseQuery");
    });

    fireEvent.click(screen.getByRole("button", { name: "功能参数" }));
    fireEvent.click(screen.getByRole("button", { name: "设置单体逆合成反推参数" }));

    expect(screen.getByLabelText("目标单体 SMILES")).toBeTruthy();
    expect(screen.getByRole("button", { name: "运行反推" })).toBeTruthy();
    expect(onOpenModule).toHaveBeenCalledTimes(1);
  });

  it("结构提示和候选数使用相同的固定高度控件壳", () => {
    render(
      <StructureWorkbenchPage
        structure={makeStructure()}
        onBackHome={vi.fn()}
        onOpenModule={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "功能参数" }));
    fireEvent.click(screen.getByRole("button", { name: "设置单体逆合成反推参数" }));

    const roleControl = screen.getByLabelText("反推结构提示").parentElement;
    const countControl = screen.getByLabelText("反推候选数").parentElement;

    expect(roleControl?.classList.contains("sw-retro-control")).toBe(true);
    expect(countControl?.classList.contains("sw-retro-control")).toBe(true);
  });
});
