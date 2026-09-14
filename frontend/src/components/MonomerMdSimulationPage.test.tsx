import { StructureWorkspace } from "../structure/workspace";
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MonomerMdJobResponse, StructureWorkspaceContext } from "../types";

const hook = vi.hoisted(() => ({
  useMonomerMdSimulation: vi.fn()
}));

vi.mock("../hooks/useMonomerMdSimulation", async () => {
  const actual = await vi.importActual<
    typeof import("../hooks/useMonomerMdSimulation")
  >("../hooks/useMonomerMdSimulation");
  return {
    ...actual,
    useMonomerMdSimulation: hook.useMonomerMdSimulation
  };
});

import { useMonomerMdSimulation } from "../hooks/useMonomerMdSimulation";
import { MonomerMdSimulationPage } from "./MonomerMdSimulationPage";

type Simulation = ReturnType<typeof useMonomerMdSimulation>;

const queuedJob: MonomerMdJobResponse = {
  job_id: "formal-b",
  status: "submitted",
  protocol: "Density",
  run_mode: "formal",
  queue_position: 2,
  created_at: "2026-07-29T00:00:00Z",
  progress_percent: 0,
  progress_stage: "queued"
};

function simulationState(overrides: Partial<Simulation> = {}): Simulation {
  return {
    historyQuery: {
      run_mode: "formal",
      page: 1,
      page_size: 10,
      protocol: "",
      status: ""
    },
    isLoading: false,
    isSubmitting: false,
    isJobLoading: false,
    error: null,
    jobLoadErrorKind: null,
    data: null,
    job: null,
    serviceStatus: {
      enabled: true,
      available: true,
      can_submit: true,
      default_steps: 300,
      formal_can_submit: true,
      formal_running_jobs: 1,
      formal_queued_jobs: 2,
      formal_max_running_jobs: 1,
      formal_max_queued_jobs: 2
    },
    protocolCatalog: {
      enabled: true,
      available: true,
      protocols: [
        {
          protocol: "Density",
          run_mode: "formal",
          runtime_ready: true
        }
      ],
      message: "ready"
    },
    isStatusLoading: false,
    statusError: null,
    protocolsError: null,
    artifactDeleteError: null,
    activeJobs: [queuedJob],
    isActiveJobsLoading: false,
    activeJobsError: null,
    history: {
      items: [queuedJob],
      total: 21,
      page: 1,
      page_size: 10
    },
    isHistoryLoading: false,
    historyError: null,
    cancellingJobIds: [],
    deletingJobIds: [],
    deleteJobErrors: {},
    submit: vi.fn().mockResolvedValue(null),
    refreshStatus: vi.fn(),
    refreshActiveJobs: vi.fn(),
    refreshHistory: vi.fn(),
    loadJob: vi.fn(),
    selectJob: vi.fn(),
    clearSelectedJob: vi.fn(),
    cancelJob: vi.fn(),
    changeHistoryQuery: vi.fn(),
    deleteArtifacts: vi.fn(),
    deleteJobRecord: vi.fn(),
    ...overrides
  };
}

const structure: StructureWorkspaceContext = {
  smiles: "",
  setSmiles: vi.fn(),
  workspace: new StructureWorkspace(""),
  getCurrentSmiles: vi.fn().mockResolvedValue("")
};

function renderPage() {
  return render(
    <MonomerMdSimulationPage
      structure={structure}
      initialJobId={null}
      onJobIdChange={vi.fn()}
      onEditStructure={vi.fn()}
    />
  );
}

describe("MonomerMdSimulationPage formal queue", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
    hook.useMonomerMdSimulation.mockReturnValue(simulationState());
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("uses the shared work-surface title hierarchy and describes the real demo without repeating its step count", () => {
    const view = renderPage();

    expect(screen.getByRole("heading", { name: "单体 MD 任务配置" })).toBeTruthy();
    expect(screen.getByRole("main", { name: "单体 MD 主工作区" }).classList.contains("np-sw-accented-surface")).toBe(true);
    expect(screen.getByText("提交快速Density模拟演示，或正式MD模拟任务")).toBeTruthy();
    expect(screen.queryByText("真实异步模拟工作台")).toBeNull();
    expect(screen.getByText(/Worker 真实执行 ByteFF2 NPT 分子动力学/)).toBeTruthy();
    expect(screen.queryByText(/Worker 真实执行 300 步 ByteFF2/)).toBeNull();
    expect(screen.getByRole("heading", { name: "真实密度演示" })).toBeTruthy();
    expect(screen.getByText("真实计算，但尚未平衡")).toBeTruthy();
    expect(screen.queryByText(/不使用预设结果/)).toBeNull();
    expect(screen.queryByText("真实模拟任务")).toBeNull();
    expect(screen.getByText("真实模拟可提交")).toBeTruthy();
    expect(screen.getByRole("button", { name: "开始快速模拟" })).toBeTruthy();
    const readyStatus = screen.getByRole("status");
    expect(readyStatus.querySelector(".np-mmd-ready-dot")).toBeTruthy();
    expect(readyStatus.querySelector("svg")).toBeNull();
    expect(readyStatus.querySelector("small")).toBeNull();
    expect(readyStatus.getAttribute("title")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /完整MD模拟/ }));
    expect(screen.getByText("ByteFF2 · 五种科研物性任务")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "选择完整 MD 模拟类型" })).toBeTruthy();

    fireEvent.click(screen.getByRole("tab", { name: /任务中心/ }));
    expect(screen.getByRole("heading", { name: "全局 MD 任务中心" })).toBeTruthy();
    expect(view.container.querySelector(".np-mmd-view-badge")).toBeTruthy();
  });

  it("renders queue/history state and applies both filters and pagination", () => {
    const simulation = simulationState();
    hook.useMonomerMdSimulation.mockReturnValue(simulation);

    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /任务中心/ }));

    expect(screen.getAllByText(/队列第 2 位/).length).toBeGreaterThan(0);
    expect(screen.getByText("第 1 / 3 页 · 共 21 项 · 每页 10 条")).toBeTruthy();

    const filters = screen.getAllByRole("combobox");
    fireEvent.click(filters[0]);
    expect(filters[0].getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByRole("option", { name: "Density" }));
    fireEvent.click(filters[1]);
    fireEvent.click(screen.getByRole("option", { name: "已取消" }));
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));

    expect(simulation.changeHistoryQuery).toHaveBeenNthCalledWith(1, {
      page: 1,
      protocol: "Density"
    });
    expect(simulation.changeHistoryQuery).toHaveBeenNthCalledWith(2, {
      page: 1,
      status: "cancelled"
    });
    expect(simulation.changeHistoryQuery).toHaveBeenNthCalledWith(3, {
      page: 2
    });
  });

  it("renders at most ten formal history records on each page", () => {
    const historyItems = Array.from({ length: 12 }, (_, index) => ({
      ...queuedJob,
      job_id: `history-${String(index + 1).padStart(2, "0")}`,
      status: "completed" as const,
      queue_position: null,
      progress_percent: 100
    }));
    hook.useMonomerMdSimulation.mockReturnValue(simulationState({
      activeJobs: [],
      history: { items: historyItems, total: 12, page: 1, page_size: 10 }
    }));

    const view = renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /任务中心/ }));

    expect(view.container.querySelectorAll(".np-mmd-history .np-mmd-task-row")).toHaveLength(10);
    expect(screen.getByText("第 1 / 2 页 · 共 12 项 · 每页 10 条")).toBeTruthy();
    expect(screen.queryByText("history-11")).toBeNull();
  });

  it("does not cancel when confirmation is rejected and sends once when accepted", () => {
    const simulation = simulationState();
    hook.useMonomerMdSimulation.mockReturnValue(simulation);
    const confirm = vi.spyOn(window, "confirm");

    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /任务中心/ }));
    const cancel = screen.getAllByRole("button", { name: "取消排队" })[0];

    confirm.mockReturnValueOnce(false);
    fireEvent.click(cancel);
    expect(simulation.cancelJob).not.toHaveBeenCalled();

    confirm.mockReturnValueOnce(true);
    fireEvent.click(cancel);
    expect(simulation.cancelJob).toHaveBeenCalledOnce();
    expect(simulation.cancelJob).toHaveBeenCalledWith(queuedJob);
  });

  it("requires confirmation before deleting a terminal record", () => {
    const terminalJob: MonomerMdJobResponse = {
      ...queuedJob,
      status: "completed",
      queue_position: null,
      progress_percent: 100
    };
    const simulation = simulationState({
      job: terminalJob,
      history: { items: [], total: 0, page: 1, page_size: 10 }
    });
    hook.useMonomerMdSimulation.mockReturnValue(simulation);
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);

    renderPage();
    fireEvent.click(screen.getByRole("tab", { name: /结果分析/ }));
    const button = screen.getByRole("button", { name: "删除记录" });
    fireEvent.click(button);
    expect(simulation.deleteJobRecord).not.toHaveBeenCalled();
    fireEvent.click(button);

    expect(confirm).toHaveBeenCalledTimes(2);
    expect(simulation.deleteJobRecord).toHaveBeenCalledTimes(1);
    expect(simulation.deleteJobRecord).toHaveBeenCalledWith(terminalJob);
  });
});
