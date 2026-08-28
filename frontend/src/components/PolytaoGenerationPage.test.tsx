// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_POLYTAO_DESCRIPTORS } from "../hooks/usePolytaoGeneration";
import {
  POLYTAO_DESCRIPTOR_NAMES,
  type PolytaoDescriptorMap,
  type PolytaoGenerationRequest,
  type PolytaoGenerationResponse,
  type PolytaoJobStatusResponse,
  type PolytaoStatusResponse,
  type StructureWorkspaceContext
} from "../types";
import { PolytaoGenerationPage } from "./PolytaoGenerationPage";

const api = vi.hoisted(() => ({
  calculatePolytaoDescriptors: vi.fn(),
  createPolytaoJob: vi.fn(),
  fetchPolytaoJob: vi.fn(),
  fetchPolytaoStatus: vi.fn(),
  fetchStructure2D: vi.fn()
}));

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    calculatePolytaoDescriptors: api.calculatePolytaoDescriptors,
    createPolytaoJob: api.createPolytaoJob,
    fetchPolytaoJob: api.fetchPolytaoJob,
    fetchPolytaoStatus: api.fetchPolytaoStatus,
    fetchStructure2D: api.fetchStructure2D
  };
});

vi.mock("./StructurePreview3D", () => ({
  StructurePreview3D: ({ smiles }: { smiles: string }) => (
    <div data-testid="structure-preview-3d">{smiles}</div>
  )
}));

const GROUPS = [
  { name: "规模与柔性", count: 4 },
  { name: "氢键能力", count: 4 },
  { name: "环结构组成", count: 7 }
] as const;

const PREFILL_DESCRIPTORS = Object.fromEntries(
  POLYTAO_DESCRIPTOR_NAMES.map((name, index) => [name, 100 + index])
) as PolytaoDescriptorMap;
const PREFILL_PROMPT = POLYTAO_DESCRIPTOR_NAMES.map((name) => PREFILL_DESCRIPTORS[name]).join(",");
const STRUCTURE_SVG = "<?xml version=\"1.0\"?><svg xmlns=\"http://www.w3.org/2000/svg\"><path d=\"M0 0\"/></svg>";
let resizeObserverCallback: ResizeObserverCallback | null = null;
let twoKMatches = false;
const mediaListeners = new Set<(event: MediaQueryListEvent) => void>();

function readyStatus(): PolytaoStatusResponse {
  return {
    enabled: true,
    available: true,
    worker_base_url_configured: true,
    worker_status: "ready",
    worker_mode: "local",
    db_configured: true,
    db_ready: true,
    db_error: null,
    runtime_ready: true,
    runtime_error: null,
    active_jobs: 0,
    model_id: "polytao",
    model_revision: "test",
    default_params: {},
    worker_version: "test",
    message: "PolyTAO runtime is ready"
  };
}

function generationResult(): PolytaoGenerationResponse {
  return {
    prompt: PREFILL_PROMPT,
    query_time_ms: 12,
    requested_count: 10,
    returned_count: 1,
    attempts: 1,
    filter_counter: {},
    results: [
      {
        rank: 1,
        generated_smiles: "*CCOC(=O)CC*",
        raw_smiles: "raw-output-hidden",
        structure_svg: STRUCTURE_SVG,
        valid_smiles: true,
        sa_score: 2.31,
        warnings: ["detail-hidden"]
      }
    ]
  };
}

function completedJob(jobId: string, result: PolytaoGenerationResponse | null = null): PolytaoJobStatusResponse {
  return {
    job_id: jobId,
    status: "completed",
    input_smiles: "canonical-prefill",
    canonical_smiles: "canonical-prefill",
    prompt: PREFILL_PROMPT,
    requested_count: 10,
    returned_count: result?.returned_count ?? 0,
    attempts: 1,
    progress_percent: 100,
    progress_stage: "completed",
    progress_message: "Completed",
    created_at: "2026-07-30T00:00:00Z",
    updated_at: "2026-07-30T00:00:01Z",
    started_at: "2026-07-30T00:00:00Z",
    finished_at: "2026-07-30T00:00:01Z",
    worker_id: "test-worker",
    worker_job_id: "test-worker-job",
    worker_version: "test",
    engine: "polytao-backend",
    error_message: null,
    result
  };
}

function makeStructure(overrides: Partial<StructureWorkspaceContext> = {}): StructureWorkspaceContext {
  return {
    smiles: "CCO",
    setSmiles: vi.fn(),
    iframeRef: { current: null },
    setIsReady: vi.fn(),
    getCurrentSmiles: vi.fn().mockResolvedValue("C(C)O"),
    ...overrides
  };
}

function renderPage(structure = makeStructure()) {
  return render(
    <PolytaoGenerationPage
      structure={structure}
      onEditStructure={vi.fn()}
      onBackHome={vi.fn()}
    />
  );
}

function descriptorRegion() {
  return screen.getByRole("region", { name: "目标分子特征" });
}

function referenceRegion() {
  return screen.getByRole("region", { name: "参考结构（可选）" });
}

function openReferenceSection() {
  const region = referenceRegion();
  const toggle = within(region).getByRole("button", { name: /已设置 · 共享结构.*展开/ });
  fireEvent.click(toggle);
  return region;
}

function openParameterPanel() {
  fireEvent.click(screen.getByRole("button", { name: "参数配置" }));
  return screen.getByRole("dialog", { name: "参数配置" });
}

async function loadSampleAndOpenParameters() {
  fireEvent.click(screen.getByRole("button", { name: "载入示例" }));
  const panel = openParameterPanel();
  const submit = within(panel).getByRole("button", { name: "开始生成" }) as HTMLButtonElement;
  await waitFor(() => expect(submit.disabled).toBe(false));
  return { panel, submit };
}

function resizePolytaoContainer(width: number) {
  act(() => {
    resizeObserverCallback?.(
      [{ contentRect: { width } } as unknown as ResizeObserverEntry],
      {} as ResizeObserver
    );
  });
}

function setTwoKViewport(matches: boolean) {
  twoKMatches = matches;
  act(() => {
    for (const listener of mediaListeners) {
      listener({ matches, media: "(min-width: 2000px) and (min-height: 1120px)" } as MediaQueryListEvent);
    }
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  resizeObserverCallback = null;
  twoKMatches = false;
  mediaListeners.clear();
  Object.defineProperty(globalThis, "ResizeObserver", {
    configurable: true,
    value: class MockResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        resizeObserverCallback = callback;
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((media: string) => ({
      get matches() {
        return twoKMatches;
      },
      media,
      onchange: null,
      addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        mediaListeners.add(listener);
      },
      removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
        mediaListeners.delete(listener);
      },
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn()
    }))
  });
  api.fetchPolytaoStatus.mockResolvedValue(readyStatus());
  api.fetchStructure2D.mockResolvedValue({ structure_svg: STRUCTURE_SVG });
  api.calculatePolytaoDescriptors.mockResolvedValue({
    input_smiles: "C(C)O",
    canonical_smiles: "canonical-prefill",
    descriptors: POLYTAO_DESCRIPTOR_NAMES.map((name) => ({
      name,
      value: PREFILL_DESCRIPTORS[name]
    })),
    prompt: PREFILL_PROMPT,
    query_time_ms: 3
  });
  api.createPolytaoJob.mockResolvedValue({
    job_id: "polytao-test-job",
    status: "submitted"
  });
  api.fetchPolytaoJob.mockImplementation(async (jobId: string) => completedJob(jobId));
});

afterEach(() => {
  cleanup();
});

describe("PolytaoGenerationPage", () => {
  it("renders the renamed page and all 15 descriptor fields in three expanded groups", () => {
    renderPage();

    expect(screen.getByRole("heading", { name: "聚合物生成" })).toBeTruthy();
    const referenceToggle = within(referenceRegion()).getByRole("button", { name: /已设置 · 共享结构.*展开/ });
    expect(referenceToggle.getAttribute("aria-expanded")).toBe("false");
    expect(api.fetchStructure2D).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: "聚合物生成结果" })).toBeNull();
    expect(screen.queryByRole("button", { name: "打开聚合物生成结果" })).toBeNull();
    const region = descriptorRegion();
    expect(within(region).getAllByRole("spinbutton")).toHaveLength(15);
    for (const name of POLYTAO_DESCRIPTOR_NAMES) {
      expect(within(region).getByText(name)).toBeTruthy();
    }
    for (const group of GROUPS) {
      const groupRegion = within(region).getByRole("region", { name: group.name });
      expect(within(groupRegion).getByRole("heading", { name: group.name })).toBeTruthy();
      expect(within(groupRegion).getByText(`0 / ${group.count} 已填写`).classList.contains("is-incomplete")).toBe(true);
    }
    expect(screen.queryByText("Prompt Preview")).toBeNull();
    expect(screen.queryByRole("button", { name: "刷新状态" })).toBeNull();
  });

  it("shows an unset reference summary without starting a preview request", () => {
    renderPage(makeStructure({ smiles: "", getCurrentSmiles: vi.fn().mockResolvedValue("") }));

    const toggle = within(referenceRegion()).getByRole("button", { name: /未设置.*展开/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(api.fetchStructure2D).not.toHaveBeenCalled();
    expect(within(referenceRegion()).getByRole("button", { name: "查看 3D" })).toHaveProperty("disabled", true);
  });

  it("loads and clears the sample vector while updating completion and source states", () => {
    renderPage();
    const region = descriptorRegion();

    fireEvent.click(screen.getByRole("button", { name: "载入示例" }));

    expect(screen.getByText("15 / 15 完整")).toBeTruthy();
    expect(screen.getByText("示例向量 · 无骨架关联")).toBeTruthy();
    expect(within(region).getByLabelText("分子量 MolWt")).toHaveProperty("value", "264");
    for (const group of GROUPS) {
      const groupRegion = within(region).getByRole("region", { name: group.name });
      expect(
        within(groupRegion)
          .getByText(`${group.count} / ${group.count} 已填写`)
          .classList.contains("is-complete")
      ).toBe(true);
    }

    fireEvent.click(screen.getByRole("button", { name: "清空" }));

    expect(screen.getByText("0 / 15 已填写")).toBeTruthy();
    expect(screen.getByText("描述符待填写")).toBeTruthy();
    expect(within(region).getAllByRole("spinbutton").every((input) => (input as HTMLInputElement).value === "")).toBe(true);
  });

  it("marks a structure-derived vector as manually adjusted after any descriptor edit", async () => {
    renderPage();

    openReferenceSection();
    fireEvent.click(screen.getByRole("button", { name: "提取描述符" }));
    await screen.findByText("目标特征源自参考结构");

    fireEvent.change(screen.getByLabelText("重原子数 HeavyAtomCount"), { target: { value: "119" } });

    expect(screen.getByText("目标特征已人工调整")).toBeTruthy();
    expect(screen.getByLabelText("重原子数 HeavyAtomCount")).toHaveProperty("value", "119");
  });

  it("prefills from the shared structure and submits the complete ordered payload", async () => {
    const structure = makeStructure();
    renderPage(structure);

    openReferenceSection();
    fireEvent.click(screen.getByRole("button", { name: "提取描述符" }));
    await waitFor(() => {
      expect(api.calculatePolytaoDescriptors).toHaveBeenCalledWith({ smiles: "C(C)O" });
      expect(screen.getByText("15 / 15 完整")).toBeTruthy();
    });

    const panel = openParameterPanel();
    const submit = within(panel).getByRole("button", { name: "开始生成" }) as HTMLButtonElement;
    await waitFor(() => expect(submit.disabled).toBe(false));
    fireEvent.click(submit);

    await waitFor(() => expect(api.createPolytaoJob).toHaveBeenCalledOnce());
    const submitted = api.createPolytaoJob.mock.calls[0][0] as PolytaoGenerationRequest;
    expect(Object.keys(submitted.descriptors)).toEqual([...POLYTAO_DESCRIPTOR_NAMES]);
    expect(submitted.descriptors).toEqual(PREFILL_DESCRIPTORS);
    expect(submitted.input_smiles).toBe("canonical-prefill");
    await waitFor(() => expect(api.fetchPolytaoJob).toHaveBeenCalledWith("polytao-test-job"));
  });

  it("clears the prefilled SMILES association after manual editing or sample loading", async () => {
    renderPage();

    openReferenceSection();
    fireEvent.click(screen.getByRole("button", { name: "提取描述符" }));
    await screen.findByText("目标特征源自参考结构");
    fireEvent.change(screen.getByLabelText("重原子数 HeavyAtomCount"), { target: { value: "119" } });

    let controls = openParameterPanel();
    await waitFor(() => expect((within(controls).getByRole("button", { name: "开始生成" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(controls).getByRole("button", { name: "开始生成" }));
    await waitFor(() => expect(api.createPolytaoJob).toHaveBeenCalledOnce());
    expect((api.createPolytaoJob.mock.calls[0][0] as PolytaoGenerationRequest).input_smiles).toBeNull();

    cleanup();
    api.createPolytaoJob.mockClear();
    renderPage();
    openReferenceSection();
    fireEvent.click(screen.getByRole("button", { name: "提取描述符" }));
    await screen.findByText("目标特征源自参考结构");
    fireEvent.click(screen.getByRole("button", { name: "载入示例" }));
    controls = openParameterPanel();
    await waitFor(() => expect((within(controls).getByRole("button", { name: "开始生成" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(controls).getByRole("button", { name: "开始生成" }));
    await waitFor(() => expect(api.createPolytaoJob).toHaveBeenCalledOnce());
    expect((api.createPolytaoJob.mock.calls[0][0] as PolytaoGenerationRequest).input_smiles).toBeNull();
    expect((api.createPolytaoJob.mock.calls[0][0] as PolytaoGenerationRequest).descriptors).toEqual(DEFAULT_POLYTAO_DESCRIPTORS);
  });

  it("validates sampling ranges inside the parameter popover", async () => {
    renderPage();
    const { panel, submit } = await loadSampleAndOpenParameters();

    const temperature = within(panel).getByLabelText("Temperature") as HTMLInputElement;
    fireEvent.change(temperature, { target: { value: "3" } });
    expect(submit.disabled).toBe(true);
    expect(temperature.classList.contains("is-invalid")).toBe(true);

    fireEvent.change(temperature, { target: { value: "1" } });
    expect(submit.disabled).toBe(false);
  });

  it("loads and caches the 2D preview only after expanding, while retaining disclosure state", async () => {
    renderPage();

    expect(api.fetchStructure2D).not.toHaveBeenCalled();
    const sourceRegion = openReferenceSection();
    expect(api.fetchStructure2D).toHaveBeenCalledWith("CCO", expect.any(AbortSignal));
    expect(await screen.findByAltText("共享聚合物重复单元二维结构")).toBeTruthy();
    const smilesToggle = screen.getByRole("button", { name: /共享结构 SMILES/ });
    expect(smilesToggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("CCO")).toBeNull();
    fireEvent.click(smilesToggle);
    expect(smilesToggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("CCO")).toBeTruthy();

    fireEvent.click(within(sourceRegion).getByRole("button", { name: /已设置 · 共享结构.*收起/ }));
    expect(screen.queryByText("CCO")).toBeNull();
    fireEvent.click(within(sourceRegion).getByRole("button", { name: /已设置 · 共享结构.*展开/ }));
    expect(screen.getByText("CCO")).toBeTruthy();
    expect(api.fetchStructure2D).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "查看 3D" }));
    expect(screen.getByRole("button", { name: "返回 2D" })).toBeTruthy();
    expect(screen.getByTestId("structure-preview-3d").textContent).toBe("CCO");
  });

  it("invalidates the cached 2D preview when the shared structure changes", async () => {
    const onEditStructure = vi.fn();
    const view = render(
      <PolytaoGenerationPage
        structure={makeStructure()}
        onEditStructure={onEditStructure}
        onBackHome={vi.fn()}
      />
    );
    openReferenceSection();
    await screen.findByAltText("共享聚合物重复单元二维结构");
    expect(api.fetchStructure2D).toHaveBeenCalledTimes(1);

    view.rerender(
      <PolytaoGenerationPage
        structure={makeStructure({ smiles: "CCC" })}
        onEditStructure={onEditStructure}
        onBackHome={vi.fn()}
      />
    );
    await waitFor(() => {
      expect(api.fetchStructure2D).toHaveBeenCalledWith("CCC", expect.any(AbortSignal));
    });
    expect(api.fetchStructure2D).toHaveBeenCalledTimes(2);
    expect(within(referenceRegion()).getByRole("button", { name: /已设置 · 共享结构.*收起/ })).toBeTruthy();
  });

  it("reopens the reference section when descriptor extraction fails", async () => {
    let rejectDescriptor!: (reason: Error) => void;
    api.calculatePolytaoDescriptors.mockImplementationOnce(
      () => new Promise((_resolve, reject) => {
        rejectDescriptor = reject;
      })
    );
    renderPage();

    const sourceRegion = openReferenceSection();
    expect(await within(sourceRegion).findByAltText("共享聚合物重复单元二维结构")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "提取描述符" }));
    await waitFor(() => expect(api.calculatePolytaoDescriptors).toHaveBeenCalledOnce());
    fireEvent.click(within(sourceRegion).getByRole("button", { name: /已设置 · 共享结构.*收起/ }));

    await act(async () => {
      rejectDescriptor(new Error("Descriptor prefill failed."));
    });

    expect(await screen.findByText("Descriptor prefill failed.")).toBeTruthy();
    expect(
      within(sourceRegion).getByRole("button", { name: /已设置 · 共享结构.*收起/ }).getAttribute("aria-expanded")
    ).toBe("true");
  });

  it("shows only the candidate structure, SMILES and SA score in the result drawer", async () => {
    api.fetchPolytaoJob.mockImplementation(async (jobId: string) => completedJob(jobId, generationResult()));
    renderPage();
    const { submit } = await loadSampleAndOpenParameters();
    fireEvent.click(submit);

    expect(await screen.findByAltText("PolyTAO 候选结构 1")).toBeTruthy();
    expect(screen.getByText("2.31")).toBeTruthy();
    expect(screen.queryByText("*CCOC(=O)CC*")).toBeNull();
    const smilesToggle = screen.getByRole("button", { name: "展开候选 1 SMILES" });
    expect(smilesToggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(smilesToggle);
    expect(screen.getByRole("button", { name: "收起候选 1 SMILES" })).toBeTruthy();
    expect(screen.getByText("*CCOC(=O)CC*")).toBeTruthy();
    expect(screen.queryByText("raw-output-hidden")).toBeNull();
    expect(screen.queryByText("detail-hidden")).toBeNull();
  });

  it("creates the result drawer on submit and supports standard and 2K keyboard widths", async () => {
    const view = renderPage();
    expect(screen.queryByRole("button", { name: "打开聚合物生成结果" })).toBeNull();
    const { submit } = await loadSampleAndOpenParameters();
    fireEvent.click(submit);
    const separator = await screen.findByRole("separator", { name: "调整聚合物生成结果侧栏宽度" });
    expect(separator.getAttribute("aria-valuenow")).toBe("380");

    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("396");
    fireEvent.keyDown(separator, { key: "Home" });
    expect(separator.getAttribute("aria-valuenow")).toBe("320");
    fireEvent.keyDown(separator, { key: "End" });
    expect(separator.getAttribute("aria-valuenow")).toBe("560");

    setTwoKViewport(true);
    await waitFor(() => expect(separator.getAttribute("aria-valuenow")).toBe("720"));
    expect(separator.getAttribute("aria-valuemin")).toBe("480");
    expect(separator.getAttribute("aria-valuemax")).toBe("720");
    fireEvent.keyDown(separator, { key: "Home" });
    expect(separator.getAttribute("aria-valuenow")).toBe("480");
    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(separator.getAttribute("aria-valuenow")).toBe("504");

    fireEvent.click(within(screen.getByRole("dialog", { name: "聚合物生成结果" })).getByRole("button", { name: "关闭聚合物生成结果" }));
    const reopen = screen.getByRole("button", { name: "打开聚合物生成结果" });
    expect(reopen.classList.contains("is-visible")).toBe(true);
    expect(reopen.tabIndex).toBe(0);
    expect(separator.tabIndex).toBe(-1);
    expect(view.container.querySelector(".polytao-detail-drawer")?.hasAttribute("inert")).toBe(true);
    fireEvent.click(reopen);
    expect(screen.getByRole("dialog", { name: "聚合物生成结果" }).getAttribute("aria-hidden")).toBe("false");
    expect(separator.tabIndex).toBe(0);

    fireEvent.pointerDown(separator, { clientX: 900 });
    expect(document.body.style.userSelect).toBe("none");
    view.unmount();
    expect(document.body.style.userSelect).toBe("");
  });

  it("uses a modal overlay with focus containment below the inline container threshold", async () => {
    api.fetchPolytaoJob.mockImplementation(async (jobId: string) => completedJob(jobId, generationResult()));
    renderPage();
    resizePolytaoContainer(1024);
    const { submit } = await loadSampleAndOpenParameters();
    fireEvent.click(submit);

    const dialog = await screen.findByRole("dialog", { name: "聚合物生成结果" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(screen.queryByRole("separator", { name: "调整聚合物生成结果侧栏宽度" })).toBeNull();
    expect(document.querySelector(".polytao-page-scroll")?.hasAttribute("inert")).toBe(true);

    const close = within(dialog).getByRole("button", { name: "关闭聚合物生成结果" });
    const last = within(dialog).getByRole("button", { name: "展开候选 1 SMILES" });
    close.focus();
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(window, { key: "Tab" });
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(dialog.getAttribute("aria-hidden")).toBe("true"));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "参数配置" })));
  });
});
