// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ConditionalGenerationTgResponse,
  StructureWorkspaceContext
} from "../types";
import { ConditionalGenerationPage } from "./ConditionalGenerationPage";

const mocks = vi.hoisted(() => ({
  submit: vi.fn(),
  reset: vi.fn(),
  setRequest: vi.fn(),
  refreshStatus: vi.fn(),
  resolveSmilesForSearch: vi.fn(),
  clearCanvas: vi.fn(),
  importImageFile: vi.fn(),
  syncSmilesFromCanvas: vi.fn(),
  toggle3D: vi.fn(),
  copySmiles: vi.fn(),
  handleEditorLoad: vi.fn(),
  data: null as ConditionalGenerationTgResponse | null
}));

vi.mock("../hooks/useConditionalGeneration", () => ({
  useConditionalGeneration: () => ({
    request: {
      smiles: "",
      delta_tg: 30,
      candidate_count: 2,
      top_k: 5,
      temperature: 1
    },
    setRequest: mocks.setRequest,
    submittedRequest: null,
    isLoading: false,
    error: null,
    data: mocks.data,
    job: null,
    submit: mocks.submit,
    reset: mocks.reset
  })
}));

vi.mock("../hooks/useConditionalGenerationStatus", () => ({
  useConditionalGenerationStatus: () => ({
    serviceStatus: {
      enabled: true,
      available: true,
      model_dir: "/model/conditional-generation",
      missing_artifacts: [],
      message: "conditional generation service is available"
    },
    serviceStatusError: null,
    isStatusLoading: false,
    refreshStatus: mocks.refreshStatus
  })
}));

vi.mock("../hooks/useTgStructureCanvas", async () => {
  const actual = await vi.importActual<typeof import("../hooks/useTgStructureCanvas")>(
    "../hooks/useTgStructureCanvas"
  );
  return {
    ...actual,
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
      importImageFile: mocks.importImageFile,
      syncSmilesFromCanvas: mocks.syncSmilesFromCanvas,
      toggle3D: mocks.toggle3D,
      resolveSmilesForSearch: mocks.resolveSmilesForSearch,
      copySmiles: mocks.copySmiles
    })
  };
});

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
  mocks.data = null;
  mocks.resolveSmilesForSearch.mockResolvedValue("*CC*");
});

afterEach(() => {
  cleanup();
});

describe("ConditionalGenerationPage Tg workbench reuse", () => {
  it("renders the Tg workbench skeleton and keeps advanced sampling collapsed", () => {
    const view = render(<ConditionalGenerationPage structure={makeStructure()} />);
    const title = screen.getByRole("heading", { name: "条件聚合物生成" });

    expect(title.parentElement).toBe(view.container.firstElementChild);
    expect(screen.getByRole("button", { name: "导入图片" }).id).toBe("btn-import-img");
    expect(screen.getByRole("button", { name: "清空画布" }).id).toBe("btn-clear-canvas");
    expect(screen.getByRole("button", { name: "生成SMILES" }).id).toBe("btn-sync-canvas");
    expect(screen.getByRole("button", { name: "3D构象" }).id).toBe("btn-toggle-3d");
    expect(screen.getByRole("button", { name: "生成参数" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "AI 助手" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "生成参数" }));
    expect(screen.getAllByRole("spinbutton")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "高级采样" }).getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "高级采样" }));
    expect(screen.getAllByRole("spinbutton")).toHaveLength(4);
    expect(screen.getByRole("button", { name: "高级采样" }).querySelector("svg")).toBeTruthy();

    fireEvent.load(screen.getByTitle("条件聚合物生成结构编辑器"));
    expect(mocks.handleEditorLoad).toHaveBeenCalledOnce();
  });

  it("wires the shared Tg canvas toolbar actions to the same controls", () => {
    render(<ConditionalGenerationPage structure={makeStructure()} />);

    fireEvent.click(screen.getByRole("button", { name: "清空画布" }));
    fireEvent.click(screen.getByRole("button", { name: "生成SMILES" }));
    fireEvent.click(screen.getByRole("button", { name: "3D构象" }));
    fireEvent.click(screen.getByRole("button", { name: "复制共享 SMILES" }));

    expect(mocks.clearCanvas).toHaveBeenCalledOnce();
    expect(mocks.syncSmilesFromCanvas).toHaveBeenCalledOnce();
    expect(mocks.toggle3D).toHaveBeenCalledOnce();
    expect(mocks.copySmiles).toHaveBeenCalledOnce();

    const structureImage = new File(["image"], "seed.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("导入结构图片"), {
      target: { files: [structureImage] }
    });
    expect(mocks.importImageFile).toHaveBeenCalledWith(structureImage);
  });

  it("keeps parameter and assistant panels exclusive and submits from the parameter flyout", async () => {
    render(<ConditionalGenerationPage structure={makeStructure()} />);

    fireEvent.click(screen.getByRole("button", { name: "生成参数" }));
    const parameterPanel = document.getElementById("cg-parameter-panel");
    expect(parameterPanel?.getAttribute("aria-hidden")).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "AI 助手" }));
    expect(parameterPanel?.getAttribute("aria-hidden")).toBe("true");
    expect(document.getElementById("cg-assistant-panel")?.getAttribute("aria-hidden")).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "建议更合适的采样参数" }));
    expect((screen.getByRole("textbox", { name: "发送给 AI 助手的消息" }) as HTMLTextAreaElement).value)
      .toBe("建议更合适的采样参数");
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    expect(screen.getByText("AI 对话接口尚未接入，本次内容未发送。")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "生成参数" }));
    fireEvent.click(screen.getByRole("button", { name: "运行生成" }));

    await waitFor(() => {
      expect(mocks.submit).toHaveBeenCalledWith({
        smiles: "*CC*",
        delta_tg: 30,
        candidate_count: 2,
        top_k: 5,
        temperature: 1
      });
    });
    expect(document.querySelector(".cg-results-drawer")?.getAttribute("aria-hidden")).toBe("false");

    const separator = screen.getByRole("separator", { name: "调整候选结果抽屉宽度" });
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("396");

    fireEvent.click(screen.getAllByRole("button", { name: "关闭候选结果" })[1]);
    expect(document.querySelector(".cg-results-drawer")?.getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByRole("button", { name: "展开条件生成候选" })).toBeTruthy();
  });

  it("closes flyouts and the result drawer with Escape", async () => {
    render(<ConditionalGenerationPage structure={makeStructure()} />);

    const parameterButton = screen.getByRole("button", { name: "生成参数" });
    parameterButton.focus();
    fireEvent.click(parameterButton);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(document.getElementById("cg-parameter-panel")?.getAttribute("aria-hidden")).toBe("true");
      expect(document.activeElement).toBe(parameterButton);
    });

    fireEvent.click(parameterButton);
    fireEvent.click(screen.getByRole("button", { name: "运行生成" }));
    await waitFor(() => {
      expect(document.querySelector(".cg-results-drawer")?.getAttribute("aria-hidden")).toBe("false");
    });
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(document.querySelector(".cg-results-drawer")?.getAttribute("aria-hidden")).toBe("true");
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "展开条件生成候选" }));
    });
  });

  it("shows only candidate structure information in the result drawer", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true
    });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText }
    });
    mocks.data = {
      input_smiles: "*CC*",
      normalized_input_smiles: "[*]CC[*]",
      delta_tg: 30,
      query_time_ms: 12,
      requested_count: 2,
      returned_count: 2,
      attempts: 4,
      filter_counter: { duplicate: 1 },
      results: [
        {
          rank: 1,
          generated_smiles: "*CCC*",
          structure_svg: "<svg xmlns=\"http://www.w3.org/2000/svg\"><path d=\"M0 0\" /></svg>",
          predicted_tg: 126,
          tg_unit: "°C",
          tg_error: null,
          similarity_score: 0.5,
          sa_score: 6.888
        },
        {
          rank: 2,
          generated_smiles: "*COC*",
          structure_svg: null,
          predicted_tg: 131,
          tg_unit: "°C",
          tg_error: null,
          similarity_score: 0.3,
          sa_score: 7.002
        }
      ]
    };

    render(<ConditionalGenerationPage structure={makeStructure()} />);
    fireEvent.click(screen.getByRole("button", { name: "生成参数" }));
    fireEvent.click(screen.getByRole("button", { name: "运行生成" }));

    await waitFor(() => {
      expect(screen.getByTestId("conditional-candidate-1")).toBeTruthy();
    });
    expect(screen.getByText("*CCC*")).toBeTruthy();
    expect(screen.getByText("126.0 °C")).toBeTruthy();
    expect(screen.getByText("0.500")).toBeTruthy();
    expect(screen.getByText("6.888")).toBeTruthy();
    const candidateImage = screen.getByAltText("条件生成候选 1 的二维结构");
    expect(candidateImage.classList.contains("cg-candidate-artwork-image")).toBe(true);
    expect(candidateImage.closest(".cg-candidate-artwork")).toBeTruthy();
    expect(candidateImage.classList.contains("max-h-[150px]")).toBe(false);

    const firstCandidate = screen.getByRole("button", { name: "选择候选 #1：*CCC*" });
    const secondCandidate = screen.getByRole("button", { name: "选择候选 #2：*COC*" });
    expect(firstCandidate.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(secondCandidate);
    expect(firstCandidate.getAttribute("aria-pressed")).toBe("false");
    expect(secondCandidate.getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(screen.getAllByRole("button", { name: "复制 SMILES" })[1]);
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("*COC*");
      expect(screen.getByRole("button", { name: "已复制" })).toBeTruthy();
    });

    expect(document.body.textContent).not.toContain("生成上下文");
    expect(document.body.textContent).not.toContain("过滤诊断");
    expect(document.body.textContent).not.toContain("query_time_ms");
    expect(document.body.textContent).not.toContain("duplicate");
    expect(document.body.textContent).not.toContain("原始管线分值");
    expect(document.body.textContent).not.toContain("仅作候选内比较");
  });
});
