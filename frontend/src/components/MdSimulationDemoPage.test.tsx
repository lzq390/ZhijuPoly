import { StructureWorkspace } from "../structure/workspace";
// @vitest-environment jsdom

import { createRef } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MdDemoDefaultsResponse,
  MdDemoRunRequest,
  MdDemoRunResponse,
  StructureWorkspaceContext,
} from "../types";
import { MdSimulationDemoPage } from "./MdSimulationDemoPage";
import { MD_DEMO_EXAMPLE_SMILES } from "./md-simulation/config";
import {
  MD_SIMULATION_DRAFT_KEY,
  saveMdSimulationDraft,
} from "./md-simulation/session";

const apiMocks = vi.hoisted(() => ({
  defaults: vi.fn(),
  run: vi.fn(),
  distance: vi.fn(),
  preview: vi.fn(),
}));

vi.mock("../services/api", () => ({
  fetchMdDemoDefaults: apiMocks.defaults,
  runMdDemo: apiMocks.run,
  calculateMdDemoAtomDistance: apiMocks.distance,
  fetchStructure2D: apiMocks.preview,
  isApiRequestError: (error: unknown, status?: number) =>
    error instanceof Error &&
    "status" in error &&
    (status === undefined ||
      (error as Error & { status: number }).status === status),
}));

const defaultRequest: MdDemoRunRequest = {
  smiles: MD_DEMO_EXAMPLE_SMILES,
  temperature: 300,
  pressure: 1,
  n_atom: 1000,
  n_chain: 10,
  forcefield: "GAFF2_mod",
};

const summary = {
  primary_stage: "eq3",
  elapsed_seconds: 5,
  n_atoms: 9940,
  n_frames: 5001,
  n_chains: 10,
  final_density_g_cm3: 1.08685,
  mean_temperature_k: 299.8677,
  mean_total_energy_kcal_mol: 17376.7267,
};

const stages = [
  {
    stage_id: "eq1",
    label: "EQ1 初始平衡",
    description: "初始松弛",
    n_atoms: 9940,
    n_frames: 100,
    dt_ps: 0.5,
    n_chains: 10,
    data_file_size_bytes: 10,
    trajectory_file_size_bytes: 20,
    log_file_size_bytes: 5,
    box: { lx: 10, ly: 11, lz: 12 },
  },
  {
    stage_id: "eq3",
    label: "EQ3 生产采样",
    description: "生产采样",
    n_atoms: 9940,
    n_frames: 5001,
    dt_ps: 1,
    n_chains: 10,
    data_file_size_bytes: 10,
    trajectory_file_size_bytes: 20,
    log_file_size_bytes: 5,
    box: { lx: 20, ly: 21, lz: 22 },
  },
];

const defaults: MdDemoDefaultsResponse = {
  default_request: defaultRequest,
  available_stages: stages,
  summary,
  fixture_metadata: {
    fixture_version: 1,
    source: {
      label: "MD fixture",
      data_root_hint: "/private/path",
      generated_from: ["fixture"],
    },
  },
};

const response: MdDemoRunResponse = {
  input: defaultRequest,
  run_id: "md-demo-test",
  status: "completed",
  query_time_ms: 12.4,
  stages,
  summary,
  density_series: {
    key: "density",
    label: "Density",
    unit: "g/cm^3",
    points: [
      { time_ps: 0, value: 0.9 },
      { time_ps: 10, value: 1.08 },
    ],
  },
  thermo_series: [
    {
      key: "temp",
      label: "Temp",
      unit: "K",
      points: [
        { time_ps: 0, value: 298 },
        { time_ps: 10, value: 300 },
      ],
    },
  ],
  trajectory_preview: {
    stage_id: "eq3",
    frame_index: 5000,
    time_ps: 5000,
    sampled_points: 2,
    points: [
      { atom_id: 1, chain_id: 1, atom_type: "1", x: 0, y: 0, z: 0 },
      { atom_id: 2, chain_id: 1, atom_type: "2", x: 1, y: 0, z: 0 },
    ],
    box: { lx: 20, ly: 21, lz: 22 },
  },
  atom_distance_series: null,
  fixture_metadata: defaults.fixture_metadata,
};

class ResizeObserverMock {
  observe() {}
  disconnect() {}
}

function makeStructure(smiles = "*CO*"): StructureWorkspaceContext {
  return {
    smiles,
    setSmiles: vi.fn(),
    workspace: new StructureWorkspace(smiles),
    getCurrentSmiles: vi.fn().mockResolvedValue(smiles),
  };
}

function renderPage(structure = makeStructure(), onEditStructure = vi.fn()) {
  return {
    ...render(
      <MdSimulationDemoPage
        structure={structure}
        onEditStructure={onEditStructure}
        resultRevealDelayMs={0}
      />,
    ),
    structure,
    onEditStructure,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}

beforeEach(() => {
  window.sessionStorage.clear();
  apiMocks.defaults.mockReset().mockResolvedValue(defaults);
  apiMocks.run.mockReset().mockResolvedValue(response);
  apiMocks.distance.mockReset();
  apiMocks.preview.mockReset().mockResolvedValue({
    structure_svg:
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#fff"/><path d="M1 5h8"/></svg>',
  });
  vi.stubGlobal("ResizeObserver", ResizeObserverMock);
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    fillRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    stroke: vi.fn(),
    arc: vi.fn(),
    fill: vi.fn(),
    globalAlpha: 1,
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
  } as unknown as CanvasRenderingContext2D);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function settleDefaultsAndPreview() {
  await act(async () => {
    await Promise.resolve();
  });
  await waitFor(() => expect(apiMocks.preview).toHaveBeenCalled());
}

async function finishRun() {
  await waitFor(() => expect(screen.getByText("结果体系规模")).toBeTruthy());
  // The hook publishes data before submit() receives the result snapshot that
  // enables exploration. Wait for the user action to become available.
  await waitFor(() => expect(screen.getByRole("tab", { name: "曲线" }).hasAttribute("disabled")).toBe(false));
}

function openParameters() {
  fireEvent.click(screen.getByRole("button", { name: "参数设置" }));
  return screen.getByRole("dialog", { name: "MD 模拟参数" });
}

function startSimulation() {
  const parameters = openParameters();
  fireEvent.click(within(parameters).getByRole("button", { name: "开始模拟" }));
}

describe("MdSimulationDemoPage", () => {
  it("does not let late defaults or a refresh overwrite user input", async () => {
    const pending = deferred<MdDemoDefaultsResponse>();
    apiMocks.defaults.mockReset().mockReturnValueOnce(pending.promise);
    renderPage();
    const smiles = screen.getByLabelText("SMILES") as HTMLTextAreaElement;
    fireEvent.change(smiles, { target: { value: "*USER*" } });

    await act(async () => {
      pending.resolve(defaults);
      await pending.promise;
    });
    await waitFor(() => expect(screen.getByText("准备就绪")).toBeTruthy());
    expect(smiles.value).toBe("*USER*");

    apiMocks.defaults.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(
      screen.getByRole("button", { name: "重新加载 MD 模拟配置" }),
    );
    await waitFor(() => expect(screen.getByText("读取失败")).toBeTruthy());
    expect(smiles.value).toBe("*USER*");

    apiMocks.defaults.mockResolvedValueOnce(defaults);
    fireEvent.click(
      screen.getByRole("button", { name: "重新加载 MD 模拟配置" }),
    );
    await waitFor(() => expect(screen.getByText("准备就绪")).toBeTruthy());
    expect(smiles.value).toBe("*USER*");
  });

  it("uses the unified workbench, shared preview, and readiness status", async () => {
    const view = renderPage();
    await settleDefaultsAndPreview();
    expect(
      view.container.querySelector(".np-structure-workbench.np-md-simulation"),
    ).not.toBeNull();
    expect(
      view.container.querySelector(
        ".np-md-workbench-surface.np-sw-accented-surface",
      ),
    ).not.toBeNull();
    const workbenchSurface = view.container.querySelector(
      ".np-md-workbench-surface",
    ) as HTMLElement;
    expect(workbenchSurface.querySelector(".np-md-workspace-tabs")).not.toBeNull();
    expect(
      workbenchSurface.querySelector("#md-simulation-input-panel"),
    ).not.toBeNull();
    const inputHeader = workbenchSurface.querySelector(
      ".np-md-surface__header",
    );
    expect(inputHeader?.classList.contains("np-md-view-header")).toBe(true);
    expect(inputHeader?.querySelector(".np-md-view-badge")?.textContent).toContain(
      "真实示例结构",
    );
    expect(inputHeader?.nextElementSibling).toBe(
      workbenchSurface.querySelector(".np-md-workspace-navigation"),
    );
    expect(screen.getByText("准备就绪")).toBeTruthy();
    expect(
      screen.getByText(/结果来自该示例结构已完成的真实 MD 计算/),
    ).toBeTruthy();
    expect(screen.queryByText(/固定演示数据|前端|服务端/)).toBeNull();
    expect((screen.getByLabelText("SMILES") as HTMLTextAreaElement).value).toBe(
      MD_DEMO_EXAMPLE_SMILES,
    );
    expect(screen.queryByRole("button", { name: /Home/ })).toBeNull();
    const preview = screen.getByAltText(
      "聚合物结构的 2D 预览",
    ) as HTMLImageElement;
    const svg = decodeURIComponent(
      preview.src.slice(preview.src.indexOf(",") + 1),
    );
    expect(svg).not.toContain("<rect");
  });

  it("replaces the legacy CC placeholder with the recommended example SMILES", async () => {
    saveMdSimulationDraft({
      ...defaultRequest,
      smiles: "CC",
      pressure: 8,
      forcefield: "",
    });
    renderPage();
    await settleDefaultsAndPreview();

    expect((screen.getByLabelText("SMILES") as HTMLTextAreaElement).value).toBe(
      MD_DEMO_EXAMPLE_SMILES,
    );
    const parameters = openParameters();
    expect(
      (
        within(parameters).getByRole("spinbutton", {
          name: /目标压力/,
        }) as HTMLInputElement
      ).value,
    ).toBe("8");
    expect(
      within(parameters).getByRole("combobox", { name: /力场/ }).textContent,
    ).toContain("GAFF2_mod");
  });

  it("opens parameters from the top-right toolbar and restores focus after Escape", async () => {
    renderPage();
    await settleDefaultsAndPreview();
    const trigger = screen.getByRole("button", { name: "参数设置" });
    expect(trigger.closest(".np-md-module-toolbar")).not.toBeNull();

    fireEvent.click(trigger);
    expect(screen.getByRole("dialog", { name: "MD 模拟参数" })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(screen.queryByRole("dialog", { name: "MD 模拟参数" })).toBeNull();
  });

  it("uses a forcefield dropdown and records the selected option", async () => {
    renderPage();
    await settleDefaultsAndPreview();
    const parameters = openParameters();
    const forcefield = within(parameters).getByRole("combobox", {
      name: /力场/,
    });

    expect(forcefield.textContent).toContain("GAFF2_mod");
    expect(
      within(parameters).queryByText(
        /固定演示|参数快照|当前接口/,
      ),
    ).toBeNull();
    fireEvent.click(forcefield);
    fireEvent.click(within(parameters).getByRole("option", { name: /PCFF/ }));
    expect(forcefield.textContent).toContain("PCFF");
    expect(window.sessionStorage.getItem(MD_SIMULATION_DRAFT_KEY)).toContain(
      '"forcefield":"PCFF"',
    );
  });

  it("imports shared structure, validates fields locally, and persists a draft", async () => {
    const view = renderPage(makeStructure("*CO*"));
    await settleDefaultsAndPreview();
    fireEvent.click(screen.getByRole("button", { name: "导入共享结构" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect((screen.getByLabelText("SMILES") as HTMLTextAreaElement).value).toBe(
      "*CO*",
    );
    expect(view.structure.getCurrentSmiles).toHaveBeenCalled();

    const parameters = openParameters();
    const pressure = within(parameters).getByRole("spinbutton", {
      name: /目标压力/,
    }) as HTMLInputElement;
    fireEvent.change(pressure, { target: { value: "0" } });
    fireEvent.blur(pressure);
    expect(
      within(parameters).getByText("压力应大于 0 且不超过 100000 atm。"),
    ).toBeTruthy();
    expect(
      (
        within(parameters).getByRole("button", {
          name: "开始模拟",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    expect(window.sessionStorage.getItem(MD_SIMULATION_DRAFT_KEY)).toContain(
      '"pressure":0',
    );

    fireEvent.click(
      within(parameters).getByRole("button", { name: "收起 MD 模拟参数" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "编辑共享结构" }));
    expect(view.onEditStructure).toHaveBeenCalledOnce();
  });

  it("switches to the main result tab immediately and exposes overview, curves, and trajectory", async () => {
    const view = renderPage();
    await settleDefaultsAndPreview();
    startSimulation();
    const resultTab = screen.getByRole("tab", { name: /模拟结果/ });
    const results = view.container.querySelector(
      "#md-simulation-results-panel",
    ) as HTMLElement;
    expect(resultTab.getAttribute("aria-selected")).toBe("true");
    expect(results).not.toBeNull();
    expect(
      results.querySelector(".np-md-results-header")?.nextElementSibling,
    ).toBe(results.querySelector(".np-md-workspace-navigation"));
    expect(
      results
        .querySelector(".np-md-results-header")
        ?.classList.contains("np-md-view-header"),
    ).toBe(true);
    expect(
      results
        .querySelector(".np-md-results-header .np-md-view-badge")
        ?.textContent,
    ).toContain("真实计算结果");
    expect(within(results).getByText("结果准备进度")).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "MD 模拟结果" })).toBeNull();
    expect(apiMocks.run).toHaveBeenCalledWith(
      defaultRequest,
      expect.any(AbortSignal),
    );

    await finishRun();
    expect(within(resultTab).getByText("已完成")).toBeTruthy();
    expect(within(results).getByText("结果体系规模")).toBeTruthy();
    expect(within(results).getAllByText("9,940").length).toBeGreaterThan(0);
    expect(
      within(results).getByText("目标原子数", { selector: "dt" }),
    ).toBeTruthy();
    expect(within(results).queryByText("/private/path")).toBeNull();

    fireEvent.click(within(results).getByRole("tab", { name: "曲线" }));
    expect(
      within(results).getByRole("img", { name: "密度随时间变化曲线" }),
    ).toBeTruthy();
    expect(within(results).getAllByText("g/cm³").length).toBeGreaterThan(0);

    fireEvent.click(within(results).getByRole("tab", { name: "轨迹" }));
    expect(
      within(results).getByRole("img", { name: /最终帧原子分布/ }),
    ).toBeTruthy();
    expect(
      within(results).getByRole("spinbutton", { name: /原子\s*1\s*编号/ }),
    ).toBeTruthy();
    expect(within(results).getAllByText(/PBC/).length).toBeGreaterThan(0);
  });

  it("does not mark a failed first run as completed", async () => {
    apiMocks.run.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    renderPage();
    await settleDefaultsAndPreview();
    startSimulation();

    await waitFor(() =>
      expect(screen.getByText("MD 模拟运行失败")).toBeTruthy(),
    );
    expect(
      within(screen.getByRole("tab", { name: /模拟结果/ })).queryByText(
        "已完成",
      ),
    ).toBeNull();
  });

  it("marks successful results stale and reset removes the draft and results", async () => {
    renderPage();
    await settleDefaultsAndPreview();
    startSimulation();
    await finishRun();
    let parameters = openParameters();
    const temperature = within(parameters).getByRole("spinbutton", {
      name: /目标温度/,
    });
    fireEvent.change(temperature, { target: { value: "310" } });
    expect(
      screen.getByText("这些结果基于上一次运行；当前输入已更改。"),
    ).toBeTruthy();

    expect(screen.queryByRole("button", { name: "清空结果" })).toBeNull();
    await act(async () => {
      await Promise.resolve();
    });
    expect(
      window.sessionStorage.getItem(MD_SIMULATION_DRAFT_KEY),
    ).not.toBeNull();

    fireEvent.click(within(parameters).getByRole("button", { name: "重置" }));
    expect(window.sessionStorage.getItem(MD_SIMULATION_DRAFT_KEY)).toBeNull();
    expect(
      screen
        .getByRole("tab", { name: "结构输入" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    parameters = openParameters();
    expect(
      (
        within(parameters).getByRole("spinbutton", {
          name: /目标温度/,
        }) as HTMLInputElement
      ).value,
    ).toBe("300");
  });
});
