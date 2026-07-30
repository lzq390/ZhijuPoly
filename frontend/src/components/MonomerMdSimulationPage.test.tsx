// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MonomerMdJobResponse } from "../types";

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
    smiles: "",
    setSmiles: vi.fn(),
    runMode: "formal",
    setRunMode: vi.fn(),
    selectedProtocol: "Density",
    setSelectedProtocol: vi.fn(),
    configText: "{}",
    setConfigText: vi.fn(),
    historyQuery: {
      run_mode: "formal",
      page: 1,
      page_size: 20,
      protocol: "",
      status: ""
    },
    isLoading: false,
    isSubmitting: false,
    isJobLoading: false,
    error: null,
    data: null,
    job: null,
    serviceStatus: {
      enabled: true,
      available: true,
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
      page_size: 20
    },
    isHistoryLoading: false,
    historyError: null,
    cancellingJobIds: [],
    deletingJobIds: [],
    deleteJobErrors: {},
    submit: vi.fn(),
    reset: vi.fn(),
    refreshStatus: vi.fn(),
    refreshActiveJobs: vi.fn(),
    refreshHistory: vi.fn(),
    loadJob: vi.fn(),
    cancelJob: vi.fn(),
    changeHistoryQuery: vi.fn(),
    loadProtocolTemplate: vi.fn(),
    deleteArtifacts: vi.fn(),
    deleteJobRecord: vi.fn(),
    ...overrides
  };
}

describe("MonomerMdSimulationPage formal queue", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hook.useMonomerMdSimulation.mockReturnValue(simulationState());
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders queue/history state and applies both filters and pagination", () => {
    const simulation = simulationState();
    hook.useMonomerMdSimulation.mockReturnValue(simulation);

    render(<MonomerMdSimulationPage onBackHome={vi.fn()} />);

    expect(screen.getAllByText("队列第 2 位").length).toBeGreaterThan(0);
    expect(screen.getByText("第 1 / 2 页 · 共 21 项")).toBeTruthy();

    const filters = screen.getAllByRole("combobox");
    fireEvent.change(filters[0], { target: { value: "Density" } });
    fireEvent.change(filters[1], { target: { value: "cancelled" } });
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

  it("does not cancel when confirmation is rejected and sends once when accepted", () => {
    const simulation = simulationState();
    hook.useMonomerMdSimulation.mockReturnValue(simulation);
    const confirm = vi.spyOn(window, "confirm");

    render(<MonomerMdSimulationPage onBackHome={vi.fn()} />);
    const cancel = screen.getByRole("button", { name: "取消排队" });

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
      history: { items: [], total: 0, page: 1, page_size: 20 }
    });
    hook.useMonomerMdSimulation.mockReturnValue(simulation);
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);

    render(<MonomerMdSimulationPage onBackHome={vi.fn()} />);
    const button = screen.getByRole("button", { name: "删除记录" });
    fireEvent.click(button);
    expect(simulation.deleteJobRecord).not.toHaveBeenCalled();
    fireEvent.click(button);

    expect(confirm).toHaveBeenCalledTimes(2);
    expect(simulation.deleteJobRecord).toHaveBeenCalledTimes(1);
    expect(simulation.deleteJobRecord).toHaveBeenCalledWith(terminalJob);
  });
});
