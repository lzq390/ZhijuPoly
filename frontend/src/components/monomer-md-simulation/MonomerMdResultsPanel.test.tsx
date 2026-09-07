// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MonomerMdJobResponse, MonomerMdSimulationResult, MonomerMdTrajectoryTimeline } from "../../types";
import { MonomerMdResultsPanel } from "./MonomerMdResultsPanel";

const job: MonomerMdJobResponse = {
  job_id: "a".repeat(32),
  status: "completed",
  run_mode: "demo",
  protocol: "DensityDemo",
  progress_percent: 1,
  progress_stage: "completed",
  requested_steps: 300,
  completed_steps: 300,
  created_at: "2026-09-01T00:00:00Z"
};

const result: MonomerMdSimulationResult = {
  protocol: "DensityDemo",
  run_mode: "demo",
  summary: { final_density_g_cm3: 1.1 },
  artifacts: {
    density: { path: "/srv/private/jobs/a/density.json", size_bytes: 1024 }
  },
  not_equilibrated: true,
  physical_density_estimate: false
};

const trajectoryTimeline: MonomerMdTrajectoryTimeline = {
  schema_version: 1,
  source_frame_count: 10,
  sampled_frame_count: 2,
  total_atoms: 4,
  sampled_points: 2,
  coordinate_unit: "angstrom",
  coordinate_scale: 0.01,
  coordinate_encoding: "int16-delta-gzip-base64",
  coordinate_byte_order: "little",
  decoded_byte_length: 24,
  compressed_byte_length: 44,
  sampling_strategy: "whole_residue_component_stratified",
  atoms: [
    { atom_id: 1, chain_id: 1, atom_type: "C", residue_name: "SOL", element: "C" },
    { atom_id: 2, chain_id: 1, atom_type: "O", residue_name: "SOL", element: "O" }
  ],
  frames: [
    { frame_index: 0, time_ps: 1, box: { lx: 20, ly: 20, lz: 20, unit: "angstrom" } },
    { frame_index: 9, time_ps: 10, box: { lx: 19, ly: 19, lz: 19, unit: "angstrom" } }
  ],
  coordinates: "H4sIAAAAAAACA0thOMGgwziB8QtjBBMXgwiDHIMGgxGDDQMA3AVZwhgAAAA="
};

function renderPanel(
  resultValue: MonomerMdSimulationResult | null = result,
  jobValue: MonomerMdJobResponse = job
) {
  return render(
    <MonomerMdResultsPanel
      job={jobValue}
      result={resultValue}
      isLoading={false}
      error={null}
      cancelling={false}
      deleting={false}
      onCancel={vi.fn()}
      onDelete={vi.fn()}
      onClear={vi.fn()}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("MonomerMdResultsPanel", () => {
  it("uses user-facing copy while restoring a deep-linked task", () => {
    const view = render(
      <MonomerMdResultsPanel
        job={null}
        result={null}
        isLoading
        error={null}
        cancelling={false}
        deleting={false}
        onCancel={vi.fn()}
        onDelete={vi.fn()}
        onClear={vi.fn()}
      />
    );
    expect(screen.getByRole("heading", { name: "正在恢复任务" })).toBeTruthy();
    expect(screen.getByText("正在读取任务的最新状态和已有结果，请稍候。")).toBeTruthy();
    expect(view.container.textContent).not.toContain("?job=");
    expect(view.container.textContent).not.toContain("32位十六进制ID");
  });

  it("shows direct 0–100 progress and the mandatory demo science warning", () => {
    renderPanel();
    expect(screen.getByLabelText("任务进度 1%")).toBeTruthy();
    expect(screen.getByText("快速演示是真实 MD 计算")).toBeTruthy();
    expect(screen.getByText(/Worker 实际执行的 300 步 MD 结果/)).toBeTruthy();
  });

  it("exposes the complete start and finish timestamps on hover and keyboard focus", () => {
    const view = renderPanel(result, {
      ...job,
      started_at: "2026-08-12T09:42:26Z",
      finished_at: "2026-08-12T10:15:44Z"
    });
    const value = view.container.querySelector<HTMLElement>(".np-mmd-job-time-range");
    const fact = view.container.querySelector<HTMLElement>(".np-mmd-job-fact-with-tooltip");
    expect(value).not.toBeNull();
    expect(value?.getAttribute("title")).toBe(value?.textContent);
    expect(value?.tabIndex).toBe(0);
    expect(fact?.dataset.tooltip).toBe(value?.textContent);
  });

  it("keeps submission information and omits low-value runtime identifiers", () => {
    renderPanel(result, {
      ...job,
      gpu_device: "2",
      engine: "byteff2-density-demo-worker",
      worker_version: "0.1.0",
      byteff2_git_sha: "abc1234"
    });
    expect(screen.getByRole("heading", { name: "提交信息" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "运行环境" })).toBeNull();
    expect(screen.queryByText("GPU 设备编号")).toBeNull();
    expect(screen.queryByText("byteff2-density-demo-worker")).toBeNull();
    expect(screen.queryByText("abc1234")).toBeNull();
  });

  it("shows dielectric implicit defaults only when protocol fields are absent", () => {
    renderPanel(null, {
      ...job,
      run_mode: "formal",
      protocol: "Dielectric",
      requested_steps: 8_000_000,
      completed_steps: 8_000_000,
      config_json: {
        protocol: "Dielectric",
        temperature: 298,
        natoms: 10_000,
        components: { Sample: 1 },
        smiles: { Sample: "CCO" }
      }
    });
    expect(screen.getByText("2,000,000 · 隐式缺省")).toBeTruthy();
    expect(screen.getByText("6,000,000 · 隐式缺省")).toBeTruthy();
    expect(screen.getByText("500 · 隐式缺省")).toBeTruthy();
    expect(screen.getAllByText("Dielectric").length).toBeGreaterThan(0);
    expect(screen.queryByText("介电性质")).toBeNull();
  });

  it("shows the HVap-only single-component constraint for a real job", () => {
    renderPanel(null, {
      ...job,
      run_mode: "formal",
      protocol: "HVap",
      requested_steps: 6_500_000,
      completed_steps: 6_500_000,
      config_json: {
        protocol: "HVap",
        temperature: 298,
        natoms: 10_000,
        components: { Sample: 1 },
        smiles: { Sample: "CCO" }
      }
    });
    expect(screen.getByText("体系约束")).toBeTruthy();
    expect(screen.getByText("单组分")).toBeTruthy();
    expect(screen.queryByText("参数来源")).toBeNull();
    expect(screen.queryByText("预计总步数")).toBeNull();
  });

  it("does not expose the raw output file inventory", () => {
    const view = renderPanel();
    expect(screen.queryByRole("tab", { name: /输出文件/ })).toBeNull();
    expect(screen.queryByText("density.json")).toBeNull();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(view.container.textContent).not.toContain("/srv/private");
  });

  it("renders explicit empty states for missing series and conformation", () => {
    renderPanel({ summary: {}, artifacts: [] });
    fireEvent.click(screen.getByRole("tab", { name: /曲线/ }));
    expect(screen.getByText("密度序列为空")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /构象/ }));
    expect(screen.getByText("没有构象坐标")).toBeTruthy();
  });

  it("surfaces a visualization-level warning that is not owned by a stage", () => {
    renderPanel({
      summary: {},
      artifacts: [],
      visualization: {
        schema_version: 1,
        status: "unavailable",
        default_stage_id: "npt",
        stages: [{ stage_id: "npt", label: "NPT", warnings: [] }],
        warnings: ["VISUALIZATION_EXTRACTION_FAILED"]
      }
    });

    expect(screen.getByText("正式结果可视化后处理失败，科学指标仍然有效。")).toBeTruthy();
  });

  it("renders only total energy when decomposition data is also present", () => {
    const view = renderPanel({
      summary: {},
      artifacts: [],
      visualization: {
        schema_version: 2,
        status: "complete",
        default_stage_id: "npt",
        stages: [{
          stage_id: "npt",
          label: "NPT",
          potential_energy_series: { unit: "kcal/mol", points: [{ time_ps: 1, value: -30 }, { time_ps: 2, value: -28 }] },
          kinetic_energy_series: { unit: "kcal/mol", points: [{ time_ps: 1, value: 10 }, { time_ps: 2, value: 9 }] },
          energy_series: { unit: "kcal/mol", points: [{ time_ps: 1, value: -20 }, { time_ps: 2, value: -19 }] },
          warnings: []
        }],
        warnings: []
      }
    });

    fireEvent.click(screen.getByRole("tab", { name: /曲线/ }));
    fireEvent.click(screen.getByRole("button", { name: "能量" }));

    expect(screen.getByRole("heading", { name: "总能量" })).toBeTruthy();
    expect(screen.queryByText("势能")).toBeNull();
    expect(screen.queryByText("动能")).toBeNull();
    expect(screen.getByRole("img", { name: /总能量，当前 -19 kcal\/mol/ })).toBeTruthy();
    expect(view.container.querySelectorAll("polyline[data-series]")).toHaveLength(1);
  });

  it("decodes and navigates a bounded trajectory timeline", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      clearRect: vi.fn(),
      fillRect: vi.fn(),
      setTransform: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      fillText: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      set strokeStyle(_value: string) {},
      set fillStyle(_value: string) {},
      set lineWidth(_value: number) {},
      set font(_value: string) {},
      set globalAlpha(_value: number) {}
    } as unknown as CanvasRenderingContext2D);
    renderPanel({
      summary: {},
      artifacts: [],
      visualization: {
        schema_version: 3,
        status: "complete",
        default_stage_id: "npt",
        stages: [{
          stage_id: "npt",
          label: "NPT",
          trajectory_timeline: trajectoryTimeline,
          warnings: []
        }],
        warnings: []
      }
    });

    fireEvent.click(screen.getByRole("tab", { name: /构象/ }));
    expect(await screen.findByRole("heading", { name: "构象轨迹" })).toBeTruthy();
    expect(screen.getByText("按模拟时间展示真实 DCD 关键帧，空间尺度固定、帧间不插值；点颜色按元素区分。")).toBeTruthy();
    const legend = screen.getByLabelText("元素颜色图例");
    expect(legend.textContent).toContain("元素配色");
    expect(legend.textContent).toContain("C");
    expect(legend.textContent).toContain("O");
    expect(screen.getByText("2 个关键帧 / 10 个源帧")).toBeTruthy();
    const slider = screen.getByRole("slider", { name: "选择构象关键帧" });
    fireEvent.change(slider, { target: { value: "1" } });
    expect(screen.getByRole("img", { name: /第 2 个关键帧，源帧 9/ })).toBeTruthy();
  });

  it("lazily loads a slimmed trajectory timeline only after opening conformation", async () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      clearRect: vi.fn(),
      fillRect: vi.fn(),
      setTransform: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      set strokeStyle(_value: string) {},
      set fillStyle(_value: string) {},
      set lineWidth(_value: number) {},
      set globalAlpha(_value: number) {}
    } as unknown as CanvasRenderingContext2D);
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(trajectoryTimeline), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);
    renderPanel({
      summary: {},
      artifacts: [],
      visualization: {
        schema_version: 3,
        status: "complete",
        default_stage_id: "npt",
        stages: [{
          stage_id: "npt",
          label: "NPT",
          trajectory_timeline_available: true,
          trajectory_timeline_summary: {
            schema_version: 1,
            source_frame_count: 10,
            sampled_frame_count: 2,
            total_atoms: 4,
            sampled_points: 2,
            coordinate_unit: "angstrom",
            coordinate_encoding: "int16-delta-gzip-base64"
          },
          warnings: []
        }],
        warnings: []
      }
    });

    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: /构象/ }));
    expect(await screen.findByRole("heading", { name: "构象轨迹" })).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/monomer-md/jobs/${"a".repeat(32)}/visualization/stages/npt/trajectory`,
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    );

    fireEvent.click(screen.getByRole("tab", { name: /概览/ }));
    fireEvent.click(screen.getByRole("tab", { name: /构象/ }));
    expect(await screen.findByRole("heading", { name: "构象轨迹" })).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("switches curves and conformation together across formal stages", () => {
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      clearRect: vi.fn(),
      fillRect: vi.fn(),
      setTransform: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      fillText: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      set strokeStyle(_value: string) {},
      set fillStyle(_value: string) {},
      set lineWidth(_value: number) {},
      set font(_value: string) {},
      set globalAlpha(_value: number) {}
    } as unknown as CanvasRenderingContext2D);
    const formalResult: MonomerMdSimulationResult = {
      summary: {},
      artifacts: [],
      visualization: {
        schema_version: 1,
        status: "complete",
        default_stage_id: "nvt",
        stages: [
          {
            stage_id: "npt",
            label: "NPT 平衡阶段",
            phase: "liquid",
            density_series: { points: [{ time_ps: 1, value: 1.1 }] },
            trajectory_preview: {
              frame_index: 3,
              total_atoms: 10,
              sampled_points: 1,
              coordinate_unit: "angstrom",
              points: [{ atom_id: 1, element: "C", x: 1, y: 2, z: 3 }]
            },
            warnings: []
          },
          {
            stage_id: "nvt",
            label: "NVT 生产阶段",
            phase: "liquid",
            density_series: { points: [{ time_ps: 2, value: 2.2 }] },
            trajectory_preview: {
              frame_index: 9,
              total_atoms: 20,
              sampled_points: 1,
              coordinate_unit: "angstrom",
              points: [{ atom_id: 2, element: "O", x: 3, y: 4, z: 5 }]
            },
            warnings: []
          }
        ],
        warnings: []
      }
    };
    renderPanel(formalResult, { ...job, run_mode: "formal", protocol: "Transport" });

    fireEvent.click(screen.getByRole("tab", { name: /曲线/ }));
    expect(screen.getByRole("img", { name: "密度，当前 2.2 g/cm³" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /NPT 平衡阶段/ }));
    expect(screen.getByRole("img", { name: "密度，当前 1.1 g/cm³" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /构象/ }));
    expect(screen.getByRole("img", { name: /采样 1 个原子、总计 10 个原子/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /NVT 生产阶段/ }));
    expect(screen.getByRole("img", { name: /采样 1 个原子、总计 20 个原子/ })).toBeTruthy();
  });

  it("keeps the active tab but resets the stage when the selected job changes", () => {
    const stagedResult: MonomerMdSimulationResult = {
      summary: {},
      artifacts: [],
      visualization: {
        schema_version: 1,
        status: "complete",
        default_stage_id: "nvt",
        stages: [
          { stage_id: "npt", label: "NPT", density_series: { points: [{ value: 1.1 }] }, warnings: [] },
          { stage_id: "nvt", label: "NVT", density_series: { points: [{ value: 2.2 }] }, warnings: [] }
        ],
        warnings: []
      }
    };
    const formalJob = { ...job, run_mode: "formal" as const, protocol: "Transport" as const };
    const view = renderPanel(stagedResult, formalJob);
    fireEvent.click(screen.getByRole("tab", { name: /曲线/ }));
    fireEvent.click(screen.getByRole("button", { name: "NPT" }));
    expect(screen.getByRole("img", { name: "密度，当前 1.1 g/cm³" })).toBeTruthy();

    view.rerender(
      <MonomerMdResultsPanel
        job={{ ...formalJob, job_id: "b".repeat(32) }}
        result={stagedResult}
        isLoading={false}
        error={null}
        cancelling={false}
        deleting={false}
        onCancel={vi.fn()}
        onDelete={vi.fn()}
        onClear={vi.fn()}
      />
    );

    expect(screen.getByRole("tabpanel")).toBeTruthy();
    expect(screen.getByRole("img", { name: "密度，当前 2.2 g/cm³" })).toBeTruthy();
  });
});
