// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StructureWorkspaceContext } from "../types";
import { ReverseDesignPage } from "./ReverseDesignPage";

const mocks = vi.hoisted(() => ({
  submit: vi.fn(),
  reset: vi.fn(),
  setRequest: vi.fn(),
  resolveSmilesForSearch: vi.fn(),
  clearCanvas: vi.fn(),
  importImageFile: vi.fn(),
  syncSmilesFromCanvas: vi.fn(),
  toggle3D: vi.fn(),
  copySmiles: vi.fn(),
  handleEditorLoad: vi.fn()
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
    reset: mocks.reset
  })
}));

vi.mock("../hooks/useTgStructureCanvas", () => ({
  useTgStructureCanvas: () => ({
    fileInputRef: { current: null },
    handleEditorLoad: mocks.handleEditorLoad,
    isEditorReady: true,
    isFlipped: false,
    isFlipping: false,
    isImportingImage: false,
    isClearing: false,
    isSyncing: false,
    isBusy: false,
    feedback: null,
    setFeedback: vi.fn(),
    copyState: "idle",
    clearCanvas: mocks.clearCanvas,
    importImageFile: mocks.importImageFile,
    syncSmilesFromCanvas: mocks.syncSmilesFromCanvas,
    toggle3D: mocks.toggle3D,
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

beforeEach(() => {
  vi.clearAllMocks();
  mocks.resolveSmilesForSearch.mockResolvedValue("*CC*");
});

afterEach(() => {
  cleanup();
});

describe("ReverseDesignPage production workbench", () => {
  it("renders the root-level title and all six required toolbar controls", () => {
    const view = render(
      <ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} />
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
    render(<ReverseDesignPage structure={makeStructure()} onOpenKnowledge={vi.fn()} />);

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
});
