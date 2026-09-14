import { StructureWorkspace } from "../structure/workspace";
/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MonomerDftCapabilitiesResponse,
  MonomerDftJobCreateRequest,
  MonomerDftJobResponse,
  MonomerDftServiceStatusResponse,
  StructureWorkspaceContext
} from "../types";

const mocks = vi.hoisted(() => ({
  useMonomerDftJob: vi.fn(),
  downloadMonomerDftBundle: vi.fn(),
  standardizeSmiles: vi.fn(),
  fetchStructure2D: vi.fn()
}));

vi.mock("../hooks/useMonomerDftJob", async () => {
  const actual = await vi.importActual<typeof import("../hooks/useMonomerDftJob")>("../hooks/useMonomerDftJob");
  return { ...actual, useMonomerDftJob: mocks.useMonomerDftJob };
});

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    downloadMonomerDftBundle: mocks.downloadMonomerDftBundle,
    standardizeSmiles: mocks.standardizeSmiles,
    fetchStructure2D: mocks.fetchStructure2D
  };
});

import { useMonomerDftJob } from "../hooks/useMonomerDftJob";
import { MonomerDftPage, monomerDftRequestsMatch } from "./MonomerDftPage";

type DftController = ReturnType<typeof useMonomerDftJob>;

const capabilities: MonomerDftCapabilitiesResponse = {
  enabled: true,
  available: true,
  schema_ready: true,
  calculation_types: ["single_point", "optimization"],
  properties: ["energy", "forces", "charges", "hessian", "frequencies"],
  default_model: "aimnet2",
  models: [{
    id: "aimnet2",
    label: "AIMNet2",
    description: "AIMNet2 potential trained against DFT data.",
    available: true,
    supported_calculation_types: ["single_point", "optimization"],
    supported_properties: ["energy", "forces", "charges", "hessian", "frequencies"],
    supported_elements: ["H", "C", "N", "O"],
    supports_spin: false,
    charge_min: -5,
    charge_max: 5
  }],
  defaults: {
    conformer: { seed: 1, max_iterations: 500 },
    single_point: { properties: ["energy", "forces", "charges"] },
    optimization: { fmax_eV_per_A: 0.01, max_steps: 50, post_optimization_properties: [] }
  },
  limits: {
    max_optimization_steps: 50,
    min_optimization_steps: 10,
    max_concurrent_jobs: 1,
    max_queued_jobs: 8,
    max_active_jobs: 9
  }
};

const serviceStatus: MonomerDftServiceStatusResponse = {
  enabled: true,
  available: true,
  schema_ready: true,
  worker_status: "ready",
  runtime_ready: true,
  draining: false,
  active_jobs: 0,
  max_active_jobs: 9,
  message: ""
};

function controller(overrides: Partial<DftController> = {}): DftController {
  return {
    serviceStatus,
    capabilities,
    job: null,
    history: { items: [], page: 1, page_size: 10, total: 0 },
    historyQuery: { page: 1, page_size: 10, status: "", calculation_type: "" },
    selectedModel: null,
    pollState: "idle",
    isServiceLoading: false,
    isHistoryLoading: false,
    isJobLoading: false,
    isSubmitting: false,
    isCancelling: false,
    cancellingJobId: null,
    isDeletingArtifacts: false,
    deletingJobIds: [],
    deleteJobErrors: {},
    serviceError: null,
    historyError: null,
    jobError: null,
    refreshStatus: vi.fn(),
    refreshHistory: vi.fn(),
    changeHistoryQuery: vi.fn(),
    loadJob: vi.fn(),
    submit: vi.fn().mockResolvedValue(null),
    cancel: vi.fn(),
    rerun: vi.fn().mockResolvedValue(null),
    deleteArtifacts: vi.fn(),
    deleteJobRecord: vi.fn(),
    clearJob: vi.fn(),
    ...overrides
  };
}

function makeStructure(smiles = "CCO"): StructureWorkspaceContext {
  return {
    smiles,
    setSmiles: vi.fn(),
    workspace: new StructureWorkspace(smiles),
    getCurrentSmiles: vi.fn().mockResolvedValue("OLD-HIDDEN-CANVAS")
  };
}

function completedJob(overrides: Partial<MonomerDftJobResponse> = {}): MonomerDftJobResponse {
  return {
    job_id: "11111111-1111-4111-8111-111111111111",
    calculation_type: "single_point",
    status: "completed",
    request: {
      calculation_type: "single_point",
      input: { smiles: "CCO", net_charge: 0, multiplicity: 1, psmiles_mode: null },
      model: "aimnet2",
      conformer: { seed: 1, max_iterations: 500 },
      single_point: { properties: ["energy"] }
    },
    request_sha256: "hash",
    attempt: 1,
    queue_position: null,
    stage: "completed",
    progress_percent: 100,
    scientific_status: "completed",
    warnings: [],
    result: null,
    timings: {},
    provenance: {},
    error: null,
    artifacts: [{
      artifact_id: "scientific_result",
      name: "scientific_result.json",
      media_type: "application/json",
      size_bytes: 128,
      sha256: "a".repeat(64),
      available: true
    }],
    artifacts_state: "available",
    artifacts_deleted: false,
    cancel_requested: false,
    created_at: "2026-09-03T00:00:00Z",
    updated_at: "2026-09-03T00:00:01Z",
    finished_at: "2026-09-03T00:00:01Z",
    idempotent_replay: false,
    ...overrides
  };
}

function renderPage(structure = makeStructure()) {
  return {
    structure,
    view: render(
      <MonomerDftPage
        structure={structure}
        initialJobId={null}
        onJobIdChange={vi.fn()}
        onEditStructure={vi.fn()}
      />
    )
  };
}

describe("MonomerDftPage workbench", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/monomer-dft");
    mocks.useMonomerDftJob.mockReturnValue(controller());
    mocks.standardizeSmiles.mockImplementation(async ({ smiles }: { smiles: string }) => ({
      standardized_smiles: smiles
    }));
    mocks.downloadMonomerDftBundle.mockResolvedValue(new Blob(["result"]));
    mocks.fetchStructure2D.mockResolvedValue({ structure_svg: "<svg viewBox=\"0 0 10 10\"></svg>" });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("uses one full-height scroll workbench and exposes keyboard-operable primary tabs", () => {
    const { view } = renderPage();

    expect(view.container.querySelector(".np-dft-scroll-region")).toBeTruthy();
    expect(view.container.querySelector("iframe")).toBeNull();
    expect(screen.getByText("设置结构、计算方式与输出内容。")).toBeTruthy();
    expect(view.container.querySelector(".np-dft-method-title")).toBeNull();
    expect(view.container.querySelector(".np-dft-module-toolbar")?.firstElementChild)
      .toHaveProperty("className", "np-dft-service-status");
    expect(view.container.querySelector(".np-dft-capacity")).toBeNull();
    expect(view.container.querySelector(".np-dft-view-badge")).toBeNull();
    expect(screen.queryByText(/AIMNet|Worker|Broker/)).toBeNull();

    const calculationTypes = within(screen.getByRole("group", { name: "计算类型" })).getAllByRole("button");
    expect(calculationTypes[0].getAttribute("aria-pressed")).toBe("true");
    expect(calculationTypes[1].getAttribute("aria-pressed")).toBe("false");

    const tablist = screen.getByRole("tablist", { name: "单体 DFT 主工作区" });
    const tabs = within(tablist).getAllByRole("tab");
    expect(tabs).toHaveLength(3);
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");

    fireEvent.keyDown(tablist, { key: "End" });
    expect(screen.getByRole("tab", { name: /结果分析/ }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText("尚未选择计算结果")).toBeTruthy();

    fireEvent.keyDown(tablist, { key: "Home" });
    expect(screen.getByRole("tab", { name: /计算配置/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("fits the complete 2D drawing inside the fixed preview viewport", async () => {
    renderPage();

    const previewImage = await screen.findByRole("img", { name: "DFT 输入结构的 2D 预览" });
    const source = decodeURIComponent((previewImage as HTMLImageElement).src);
    expect(previewImage.classList.contains("h-full")).toBe(true);
    expect(previewImage.classList.contains("absolute")).toBe(true);
    expect(source).toContain('viewBox="0 0 10 10"');
  });

  it("presents calculation purposes in the styled dropdown without exposing model names", async () => {
    renderPage();
    const purposeSelect = screen.getByRole("combobox", { name: /适用体系/ });

    purposeSelect.focus();
    fireEvent.click(purposeSelect);
    const purposeOption = screen.getByRole("option", { name: /通用有机分子/ });
    expect(purposeOption).toBeTruthy();
    expect(screen.queryByText(/AIMNet/)).toBeNull();
    expect(screen.getByRole("listbox")).toBeTruthy();

    fireEvent.keyDown(purposeSelect, { key: "Tab" });
    expect(screen.queryByRole("listbox")).toBeNull();

    fireEvent.click(purposeSelect);
    fireEvent.keyDown(purposeSelect, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(purposeSelect));
  });

  it("keeps the active keyboard option visible in a long dropdown", async () => {
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView
    });
    try {
      renderPage();
      fireEvent.click(screen.getByRole("tab", { name: /任务中心/ }));
      const statusSelect = screen.getByRole("combobox", { name: /任务状态/ });
      fireEvent.click(statusSelect);
      scrollIntoView.mockClear();
      for (let index = 0; index < 7; index += 1) {
        fireEvent.keyDown(statusSelect, { key: "ArrowDown" });
      }

      await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
      expect(document.querySelector("#np-dft-history-status-listbox .is-active")?.textContent).toContain("已取消");
    } finally {
      if (originalScrollIntoView) {
        Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
          configurable: true,
          value: originalScrollIntoView
        });
      } else {
        Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
      }
    }
  });

  it("switches to an available model in the same user-facing purpose after availability changes", async () => {
    const submit = vi.fn().mockResolvedValue(null);
    const alternateModel = {
      ...capabilities.models[0],
      id: "aimnet2-2025" as const,
      label: "AIMNet2 2025"
    };
    const structure = makeStructure();
    const props = {
      structure,
      initialJobId: null,
      onJobIdChange: vi.fn(),
      onEditStructure: vi.fn()
    };
    mocks.useMonomerDftJob.mockReturnValue(controller({
      submit,
      capabilities: { ...capabilities, models: [capabilities.models[0], alternateModel] }
    }));
    const view = render(<MonomerDftPage {...props} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "提交计算" }).hasAttribute("disabled")).toBe(false));

    mocks.useMonomerDftJob.mockReturnValue(controller({
      submit,
      capabilities: {
        ...capabilities,
        models: [{ ...capabilities.models[0], available: false }, alternateModel]
      }
    }));
    view.rerender(<MonomerDftPage {...props} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "提交计算" }).hasAttribute("disabled")).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "提交计算" }));

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(submit.mock.calls[0][0].model).toBe("aimnet2-2025");
  });

  it("compares requested property selections by meaning rather than checkbox order", () => {
    const baseRequest = completedJob().request;
    if (baseRequest.calculation_type !== "single_point") throw new Error("expected single-point fixture");
    const submitted: MonomerDftJobCreateRequest = {
      ...baseRequest,
      single_point: { properties: ["energy", "charges", "forces"] }
    };
    const reordered: MonomerDftJobCreateRequest = {
      ...submitted,
      single_point: { properties: ["energy", "forces", "charges"] }
    };
    const changed: MonomerDftJobCreateRequest = {
      ...submitted,
      single_point: { properties: ["energy", "charges"] }
    };

    expect(monomerDftRequestsMatch(completedJob({ request: submitted }), reordered)).toBe(true);
    expect(monomerDftRequestsMatch(completedJob({ request: submitted }), changed)).toBe(false);
  });

  it("cancels an in-flight 2D preview as soon as the visible structure changes", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    mocks.fetchStructure2D.mockImplementation((_smiles: string, signal: AbortSignal) => {
      signals.push(signal);
      return new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    renderPage();

    await act(async () => { await vi.advanceTimersByTimeAsync(380); });
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(false);

    fireEvent.change(screen.getByLabelText("SMILES / PSMILES"), { target: { value: "CCN" } });
    expect(signals[0].aborted).toBe(true);
    expect(signals).toHaveLength(1);
  });

  it("uses a neutral unavailable status when calculation submission is not open", () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({
      serviceStatus: { ...serviceStatus, enabled: false, available: false },
      capabilities: { ...capabilities, enabled: false, available: false }
    }));
    const { view } = renderPage();

    expect(screen.getAllByText("暂未开放功能")).toHaveLength(2);
    expect(screen.getByText("计算功能暂未开放")).toBeTruthy();
    expect(view.container.querySelector(".np-dft-service-status .is-disabled .np-dft-disabled-dot")).toBeTruthy();
    expect(view.container.querySelector(".np-dft-service-status .np-dft-ready-dot")).toBeNull();
  });

  it("does not report stale readiness after a service-status check fails", () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({
      serviceError: "暂时无法确认计算服务状态，请稍后刷新。"
    }));
    const { view } = renderPage();

    expect(within(view.container.querySelector(".np-dft-module-toolbar") as HTMLElement)
      .getByText("状态检查失败")).toBeTruthy();
    expect(view.container.querySelector(".np-dft-service-status .is-error")).toBeTruthy();
    expect(view.container.querySelector(".np-dft-service-status .np-dft-ready-dot")).toBeNull();
    expect(screen.getByRole("button", { name: "提交计算" }).hasAttribute("disabled")).toBe(true);
  });

  it("keeps task counts out of the main tab and shows ten history records per page", () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({
      history: { items: [], page: 1, page_size: 10, total: 21 }
    }));
    renderPage();

    const tasksTab = screen.getByRole("tab", { name: /任务中心/ });
    expect(tasksTab.querySelector("b")).toBeNull();
    fireEvent.click(tasksTab);
    expect(screen.getByText("每页 10 条；筛选不切换当前结果。")).toBeTruthy();
  });

  it("offers one result bundle download beside the selected-task actions", () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({ job: completedJob() }));
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /结果分析/ }));
    const download = screen.getByRole("button", { name: "下载结果" });
    expect(download.hasAttribute("disabled")).toBe(false);
    expect(screen.queryByRole("tab", { name: /文件与记录/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /删除输出文件/ })).toBeNull();
  });

  it("downloads the result bundle through a controlled request and reports success", async () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({ job: completedJob() }));
    const createObjectUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:dft-result");
    const revokeObjectUrl = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const clickLink = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /结果分析/ }));
    fireEvent.click(screen.getByRole("button", { name: "下载结果" }));

    await waitFor(() => expect(mocks.downloadMonomerDftBundle).toHaveBeenCalledWith(
      "11111111-1111-4111-8111-111111111111",
      expect.any(AbortSignal)
    ));
    await screen.findByText("结果下载已开始。");
    expect(createObjectUrl).toHaveBeenCalled();
    expect(clickLink).toHaveBeenCalled();
    await waitFor(() => expect(revokeObjectUrl).toHaveBeenCalledWith("blob:dft-result"));
  });

  it("shows a user-facing error when the result bundle cannot be prepared", async () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({ job: completedJob() }));
    mocks.downloadMonomerDftBundle.mockRejectedValue(new Error("bundle service failed"));
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /结果分析/ }));
    fireEvent.click(screen.getByRole("button", { name: "下载结果" }));

    expect((await screen.findByRole("alert")).textContent).toContain("结果下载失败，请稍后重试。");
  });

  it("keeps result download visible but disabled when files are unavailable", () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({
      job: completedJob({ artifacts_state: "deleted", artifacts_deleted: true })
    }));
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /结果分析/ }));
    expect(screen.getByRole("button", { name: "下载结果" }).hasAttribute("disabled")).toBe(true);
  });

  it("locks conflicting selected-task actions while a rerun is being created", () => {
    const job = completedJob();
    mocks.useMonomerDftJob.mockReturnValue(controller({
      job,
      history: { items: [job], page: 1, page_size: 10, total: 1 },
      isSubmitting: true
    }));
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /结果分析/ }));
    expect(screen.getByRole("button", { name: "重跑同参数" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "删除记录" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "取消选择" }).hasAttribute("disabled")).toBe(true);

    fireEvent.click(screen.getByRole("tab", { name: /任务中心/ }));
    expect(screen.getAllByRole("button", { name: "删除记录" }).every((button) => button.hasAttribute("disabled"))).toBe(true);
    expect(document.querySelector(".np-dft-history-select")?.hasAttribute("disabled")).toBe(true);
  });

  it("shows an expired-task message when a deep-link lookup has no result", () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({
      jobError: "该任务已被删除或已按保留策略到期清理。"
    }));
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /结果分析/ }));
    expect(screen.getByRole("alert").textContent).toContain("该任务已被删除或已按保留策略到期清理。");
    expect(screen.getByText("尚未选择计算结果")).toBeTruthy();
  });

  it("shows a stable loading state while a selected task is being restored", () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({
      pollState: "polling",
      isJobLoading: true
    }));
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /结果分析/ }));
    expect(screen.getByText("正在读取任务")).toBeTruthy();
    expect(screen.getByText("正在同步任务状态与结果，请稍候…")).toBeTruthy();
    expect(screen.queryByText("尚未选择计算结果")).toBeNull();
  });

  it("shows selected-task action failures without duplicating task failure text", () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({
      job: completedJob(),
      jobError: "删除任务失败，请稍后重试。"
    }));
    renderPage();

    fireEvent.click(screen.getByRole("tab", { name: /结果分析/ }));
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("操作未完成");
    expect(alert.textContent).toContain("删除任务失败，请稍后重试。");
  });

  it.each(["CCN", "CC("])("restores the shared draft %s when entering DFT", (draft) => {
    const structure = makeStructure("CCO");
    structure.workspace.setDraft(draft);
    renderPage(structure);

    expect(screen.getByLabelText("SMILES / PSMILES")).toHaveProperty("value", draft);
    expect(structure.workspace.getSnapshot().smiles).toBe("CCO");
    expect(structure.getCurrentSmiles).not.toHaveBeenCalled();
  });

  it("preserves an incomplete draft when the accepted structure changes", () => {
    const structure = makeStructure("CCO");
    structure.workspace.setDraft("CC(");
    const { view } = renderPage(structure);

    act(() => structure.workspace.commitSmiles("CCN"));
    view.rerender(
      <MonomerDftPage
        structure={{ ...structure, smiles: structure.workspace.getSnapshot().smiles }}
        initialJobId={null}
        onJobIdChange={vi.fn()}
        onEditStructure={vi.fn()}
      />
    );

    expect(structure.workspace.getSnapshot().smiles).toBe("CCN");
    expect(screen.getByLabelText("SMILES / PSMILES")).toHaveProperty("value", "CC(");
  });

  it("does not submit an invalid restored draft or fall back to the accepted structure", async () => {
    const submit = vi.fn();
    mocks.useMonomerDftJob.mockReturnValue(controller({ submit }));
    mocks.standardizeSmiles.mockRejectedValue(new Error("Invalid SMILES"));
    const structure = makeStructure("CCO");
    structure.workspace.setDraft("CC(");
    renderPage(structure);

    const submitButton = screen.getByRole("button", { name: "提交计算" });
    await waitFor(() => expect(submitButton.hasAttribute("disabled")).toBe(false));
    fireEvent.click(submitButton);

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("SMILES 标准化失败"));
    expect(mocks.standardizeSmiles).toHaveBeenCalledWith({ smiles: "CC(" }, expect.any(AbortSignal));
    expect(submit).not.toHaveBeenCalled();
    expect(structure.getCurrentSmiles).not.toHaveBeenCalled();
    expect(screen.getByLabelText("SMILES / PSMILES")).toHaveProperty("value", "CC(");
  });

  it("standardizes and submits a valid restored draft without another edit", async () => {
    const submit = vi.fn().mockResolvedValue("11111111-1111-4111-8111-111111111111");
    mocks.useMonomerDftJob.mockReturnValue(controller({ submit }));
    mocks.standardizeSmiles.mockResolvedValue({ standardized_smiles: "CCO" });
    const structure = makeStructure("CC");
    structure.workspace.setDraft("C(C)O");
    renderPage(structure);

    const submitButton = screen.getByRole("button", { name: "提交计算" });
    await waitFor(() => expect(submitButton.hasAttribute("disabled")).toBe(false));
    fireEvent.click(submitButton);

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(mocks.standardizeSmiles).toHaveBeenCalledWith({ smiles: "C(C)O" }, expect.any(AbortSignal));
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      input: expect.objectContaining({ smiles: "CCO" })
    }));
    expect(structure.getCurrentSmiles).not.toHaveBeenCalled();
  });

  it("submits the standardized visible draft and never reads the hidden Ketcher value", async () => {
    const submit = vi.fn().mockResolvedValue("11111111-1111-4111-8111-111111111111");
    mocks.useMonomerDftJob.mockReturnValue(controller({ submit }));
    mocks.standardizeSmiles.mockResolvedValue({ standardized_smiles: "CCN" });
    const { structure } = renderPage();

    fireEvent.change(screen.getByLabelText("SMILES / PSMILES"), { target: { value: " C C N " } });
    fireEvent.change(screen.getByLabelText("SMILES / PSMILES"), { target: { value: "CCN" } });
    const submitButton = await screen.findByRole("button", { name: "提交计算" });
    await waitFor(() => expect(submitButton.hasAttribute("disabled")).toBe(false));
    const scrollRegion = document.querySelector(".np-dft-scroll-region") as HTMLDivElement;
    scrollRegion.scrollTop = 900;
    fireEvent.click(submitButton);

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(mocks.standardizeSmiles).toHaveBeenCalledWith({ smiles: "CCN" }, expect.any(AbortSignal));
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      calculation_type: "single_point",
      input: expect.objectContaining({ smiles: "CCN" }),
      single_point: { properties: ["energy", "forces", "charges"] }
    }));
    expect(structure.getCurrentSmiles).not.toHaveBeenCalled();
    expect(structure.setSmiles).toHaveBeenCalledWith("CCN");
    expect(screen.getByRole("tab", { name: /结果分析/ }).getAttribute("aria-selected")).toBe("true");
    expect(scrollRegion.scrollTop).toBe(0);
  });

  it("does not create a task from a late structure-standardization response after leaving the page", async () => {
    let resolveStandardization!: (value: { standardized_smiles: string }) => void;
    let requestSignal: AbortSignal | undefined;
    const pendingStandardization = new Promise<{ standardized_smiles: string }>((resolve) => {
      resolveStandardization = resolve;
    });
    const submit = vi.fn().mockResolvedValue("11111111-1111-4111-8111-111111111111");
    mocks.useMonomerDftJob.mockReturnValue(controller({ submit }));
    mocks.standardizeSmiles.mockImplementation((_payload: { smiles: string }, signal: AbortSignal) => {
      requestSignal = signal;
      return pendingStandardization;
    });
    const { view } = renderPage();

    fireEvent.click(screen.getByRole("button", { name: "提交计算" }));
    await waitFor(() => expect(mocks.standardizeSmiles).toHaveBeenCalledTimes(1));
    view.unmount();
    expect(requestSignal?.aborted).toBe(true);

    await act(async () => {
      resolveStandardization({ standardized_smiles: "CCO" });
      await pendingStandardization;
    });
    expect(submit).not.toHaveBeenCalled();
  });

  it("blocks submission when the capabilities snapshot reports a draining Worker", async () => {
    mocks.useMonomerDftJob.mockReturnValue(controller({
      capabilities: { ...capabilities, worker: { runtime_ready: true, draining: true } }
    }));
    renderPage();

    expect(screen.getAllByText("暂缓新任务")).toHaveLength(2);
    const submitButton = await screen.findByRole("button", { name: "提交计算" });
    expect(submitButton.hasAttribute("disabled")).toBe(true);
  });
});
