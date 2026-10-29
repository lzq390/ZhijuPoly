// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PolymerMdCapabilities, PolymerMdJob, PolymerMdStage } from "../../types/polymerMd";
import { defaultPolymerMdUserConfiguration, mapPolymerMdUserConfiguration } from "./userConfiguration";
import { POLYMER_MD_DEFAULTS } from "./config";
import { PolymerMdResults } from "./PolymerMdResults";
import { retireSession } from "../../auth/session";

const api = vi.hoisted(() => ({ trajectory: vi.fn(), distance: vi.fn(), logs: vi.fn(), download: vi.fn(), bundle: vi.fn() }));
vi.mock("../../services/polymerMdApi", () => ({ fetchPolymerMdTrajectory: api.trajectory, calculatePolymerMdDistance: api.distance, fetchPolymerMdStageLogs: api.logs, downloadPolymerMdArtifact: api.download }));
vi.mock("../../services/polymerMdArtifactBundle", () => ({ downloadPolymerMdArtifacts: api.bundle }));
vi.mock("../md-simulation/MdSeriesChart", () => ({ MdSeriesChart: ({ xAxisLabel }: { xAxisLabel?: string }) => <div>科学曲线{xAxisLabel ? `：${xAxisLabel}` : ""}</div> }));
vi.mock("../md-simulation/MdTrajectoryExplorer", () => ({ MdTrajectoryExplorer: ({ onSelectionChange, onCalculate, distance, selections, distanceLoading, distanceEnabled, distanceError, distanceNotice, distanceElapsedSeconds, onCancelDistance }: {
  onSelectionChange: (value: unknown) => void; onCalculate: () => void; distance: unknown; selections: Array<{ atom_id: number } | null>;
  distanceLoading: boolean; distanceEnabled: boolean; distanceError: string | null; distanceNotice?: string; distanceElapsedSeconds?: number; onCancelDistance?: () => void;
}) => <div><button onClick={() => onSelectionChange([{ atom_id: 1 }, { atom_id: 2 }])}>选取原子对</button><button onClick={() => onSelectionChange([{ atom_id: 320 }, { atom_id: 1253 }])}>选择320与1253</button><p aria-label="已选原子">{selections.map(atom => atom?.atom_id ?? "未选").join("/")}</p><button disabled={!distanceEnabled || distanceLoading} onClick={onCalculate}>读取距离</button>{distanceLoading ? <><p role="status">已等待 {distanceElapsedSeconds} 秒</p><button onClick={onCancelDistance}>停止等待分析</button></> : null}{distanceError ? <p role="alert">{distanceError}</p> : null}{distanceNotice ? <p role="status">{distanceNotice}</p> : null}{distance ? <p>距离分析已返回</p> : null}</div> }));

const windowData = { start_ps: 2100, stop_ps: 5000, requested_start_ps: 2000, observed_start_ps: 0, observed_stop_ps: 5000, sample_count: 2901 };
const fingerprint = "a".repeat(64), currentFingerprint = "b".repeat(64);
function stage(phase: string, overrides: Partial<PolymerMdStage> = {}): PolymerMdStage {
  return { stage_run_id: `${phase}-1`, stage: phase, attempt: 1, status: "completed", quality_status: "passed", results: {}, quality: {}, errors: [], ...overrides };
}
function job(overrides: Partial<PolymerMdJob> = {}): PolymerMdJob {
  return { job_id: "98f12c00-8194-4ae2-bd35-140cac6ff992", status: "completed", quality_status: "warning", scientific_request: { ...POLYMER_MD_DEFAULTS, seed: 1729 }, stage: "analyze_0", stage_runs: [], artifacts: [], results: {}, created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z", ...overrides };
}
const capabilities: PolymerMdCapabilities = { enabled: true, submit_enabled: false, schema_ready: true, worker_ready: false, protocol_version: 1, supported_forcefields: ["GAFF2_mod"], supported_terminals: ["CH3"], supported_calculations: ["basic", "basic_tc"], supported_tacticities: ["atactic"], trajectory_available: false, distance_available: false, limits: { per_owner_active: 1, global_active: 3, execution: 1, max_total_atoms: 20000, max_rnemd_atoms: 60000 }, runtime: { runtime_fingerprint: currentFingerprint } };
function page(value: PolymerMdJob, caps: PolymerMdCapabilities | null = capabilities) {
  return render(<PolymerMdResults job={value} capabilities={caps} busy={null} onCancel={vi.fn()} onClear={vi.fn()} onDelete={vi.fn()} />);
}
function expandEvidence(container: HTMLElement) {
  const root = screen.getByText("运行条件与质量证据").closest("details")!;
  expect(root.open).toBe(false);
  root.open = true;
  for (const details of container.querySelectorAll<HTMLDetailsElement>(".np-polymer-md-evidence details")) details.open = true;
  return within(root);
}

beforeEach(() => { vi.clearAllMocks(); api.bundle.mockReset(); api.download.mockReset(); });
afterEach(cleanup);

describe("polymer MD result provenance", () => {
  it("publishes slow running-stage logs before scheduling another read and stops after unmount", async () => {
    vi.useFakeTimers();
    try {
      api.logs.mockImplementation((jobId, stageId) => new Promise(resolve => setTimeout(() => resolve({
        job_id: jobId, stage_run_id: stageId, lines: ["Step 1000 Temp 300"], truncated: false,
      }), 6000)));
      const view = page(job({ status: "running", stage_runs: [stage("eq3", { status: "running" })] }));
      const logs = screen.getByText("查看EQ3 平衡采样日志").closest("details")!;
      logs.open = true;
      fireEvent(logs, new Event("toggle"));
      expect(api.logs).toHaveBeenCalledTimes(1);
      await act(() => vi.advanceTimersByTimeAsync(6000));
      expect(screen.getByText("Step 1000 Temp 300")).toBeTruthy();
      expect(api.logs).toHaveBeenCalledTimes(1);
      expect((api.logs.mock.calls[0][2] as AbortSignal).aborted).toBe(false);

      await act(() => vi.advanceTimersByTimeAsync(5000));
      expect(api.logs).toHaveBeenCalledTimes(2);
      view.unmount();
      expect((api.logs.mock.calls[1][2] as AbortSignal).aborted).toBe(true);
      await act(() => vi.advanceTimersByTimeAsync(20000));
      expect(api.logs).toHaveBeenCalledTimes(2);
    } finally {
      cleanup();
      vi.useRealTimers();
      api.logs.mockReset();
    }
  });

  it("shows code-only terminal and stage failures and opens the corresponding real log request", async () => {
    const failed = job({ status: "failed", quality_status: "failed", error_code: "stage_retries_exhausted", error_message: null,
      results: { properties: [{ key: "density", value: null, unit: "g/cm3", reason: "stage_retries_exhausted", quality_status: "failed" }] },
      stage_runs: [stage("eq1", { status: "failed", quality_status: "failed", errors: [{ code: "engine_process_failed", retryable: false, private_path: "/private/FAILURE_CANARY" }] })] });
    api.logs.mockResolvedValue({ job_id: failed.job_id, stage_run_id: "eq1-1", lines: ["Generated input validation failed"], truncated: false });
    const view = page(failed);
    expect(screen.getByText("阶段技术重试已用尽")).toBeTruthy();
    expect(screen.getByText("引擎执行失败")).toBeTruthy();
    expect(screen.getAllByText("请查看对应阶段日志。")).toHaveLength(2);
    expect(screen.getByText("不可用")).toBeTruthy();
    expect(view.container.textContent).not.toContain("FAILURE_CANARY");
    const logs = screen.getByText("查看EQ1 松弛与装填日志").closest("details")!;
    logs.open = true; fireEvent(logs, new Event("toggle"));
    await screen.findByText("Generated input validation failed");
    expect(api.logs).toHaveBeenCalledWith(failed.job_id, "eq1-1", expect.any(AbortSignal));
  });

  it("prefers supplied failure messages and bounds unknown code display without rendering private fields", () => {
    const view = page(job({ status: "failed", error_code: "stage_retries_exhausted", error_message: "自定义任务诊断",
      stage_runs: [stage("eq1", { status: "failed", errors: [
        { code: "engine_process_failed", message: "自定义阶段诊断" },
        { code: "new_engine_error", details: { token: "FAILURE_CANARY" } }, { code: "constructor" },
        { code: "/private/FAILURE_CANARY" }, { code: "x".repeat(97) }, { code: { token: "FAILURE_CANARY" } }
      ] })] }));
    expect(screen.getByText("自定义任务诊断")).toBeTruthy();
    expect(screen.getByText("自定义阶段诊断")).toBeTruthy();
    expect(screen.getByText("执行错误（new_engine_error）")).toBeTruthy();
    expect(screen.getByText("执行错误（constructor）")).toBeTruthy();
    expect(screen.queryByText("阶段技术重试已用尽")).toBeNull();
    expect(screen.queryByText("引擎执行失败")).toBeNull();
    expect(view.container.textContent).not.toContain("FAILURE_CANARY");
    expect(view.container.textContent).not.toContain("x".repeat(97));
  });

  it.each(["GAFF", "GAFF2", "GAFF2_mod", "Dreiding", "Dreiding_UT"] as const)("shows requested and actual %s alongside exact QM and additional targets", forcefield => {
    const request = mapPolymerMdUserConfiguration({ ...defaultPolymerMdUserConfiguration(), forcefield, opt_method: "HF", charge: "gasteiger", properties: ["tg", "sp_total", "thermal_diffusivity"] }, POLYMER_MD_DEFAULTS).request!;
    const view = page(job({ scientific_request: request, stage_runs: [stage("build", { results: { forcefield, actual_atom_count: 10040 } }), stage("monomer_qm", { results: { effective_native_configuration: { ...request.native_configuration, opt_method: "wb97m-d3bj" } } })] }));
    const evidence = expandEvidence(view.container);
    const saved = within(evidence.getByRole("region", { name: "持久化科学请求" }));
    expect(saved.getByText(forcefield)).toBeTruthy();
    expect(saved.getAllByText("HF（Hartree–Fock）").length).toBeGreaterThan(0);
    expect(saved.getByText(/基础物性、RNEMD 热导率、热扩散率、玻璃化转变温度 Tg、溶解度参数 SP/)).toBeTruthy();
    expect(saved.getByText(/所选电荷模型不使用量化电荷方法与基组/)).toBeTruthy();
    expect(within(evidence.getByRole("region", { name: "实际构建模型" })).getByText(forcefield)).toBeTruthy();
    expect(within(evidence.getByRole("region", { name: "阶段协议与运行时来源" })).getAllByText("ωB97M-D3(BJ)").length).toBeGreaterThan(0);
  });

  it("separates locked Br/I protocol rules from saved role applicability and actual element assignments", () => {
    const configuration = { ...defaultPolymerMdUserConfiguration(), charge: "zero", opt_basis: "6-311+G(2d,p)", properties: ["refractive_index"] };
    const request = mapPolymerMdUserConfiguration(configuration, POLYMER_MD_DEFAULTS).request!;
    const view = page(job({ scientific_request: request, stage_runs: [stage("monomer_qm", { results: {
      effective_native_configuration: request.native_configuration,
      parameter_applicability: { opt_basis: { applicable: true }, charge_basis: { applicable: false }, polar_basis: { applicable: true } }
    } })] }));
    const evidence = expandEvidence(view.container);
    const rules = within(evidence.getByRole("region", { name: "锁定协议的 Br/I 基组覆盖规则" }));
    expect(rules.getByText("当前锁定协议：Br/I 覆盖规则（v9）")).toBeTruthy();
    expect(rules.getByText(/不代表该任务采用 v9 或已经完成相应计算，也不是实测元素分配/)).toBeTruthy();
    const opt = within(rules.getByRole("rowheader", { name: "几何优化主基组" }).closest("tr")!);
    expect(opt.getByText("6-31G(d,p)")).toBeTruthy();
    expect(opt.getByText("角色适用；阶段已完成，元素分配见日志")).toBeTruthy();
    const charge = within(rules.getByRole("rowheader", { name: "电荷量化主基组" }).closest("tr")!);
    expect(charge.getByText("6-31G(d)")).toBeTruthy();
    expect(charge.getByText("保存证据：本角色不适用")).toBeTruthy();
    expect(within(rules.getByRole("rowheader", { name: "静态极化率主基组" }).closest("tr")!).getByText("6-311G(d,p)")).toBeTruthy();
    expect(rules.queryByRole("rowheader", { name: "单点电子结构主基组" })).toBeNull();
    expect(evidence.getAllByText("6-311+G(2d,p)").length).toBeGreaterThan(0);
  });

  it("does not infer execution from historical choices or an unfinished stage", () => {
    const request = mapPolymerMdUserConfiguration(defaultPolymerMdUserConfiguration(), POLYMER_MD_DEFAULTS).request!;
    const view = page(job({ scientific_request: request, stage_runs: [stage("terminal_qm", { status: "failed", results: {
      effective_native_configuration: request.native_configuration,
      parameter_applicability: { terminal_optimization: { applicable: false }, charge_basis: { applicable: true } }
    } }), stage("monomer_qm", { results: { effective_native_configuration: request.native_configuration } })] }));
    const evidence = expandEvidence(view.container);
    const [terminal, monomer] = evidence.getAllByRole("region", { name: "锁定协议的 Br/I 基组覆盖规则" }).map(element => within(element));
    expect(terminal.getByText("保存证据：本角色不适用")).toBeTruthy();
    expect(terminal.getByText("角色适用；阶段未完成，不能确认本角色已完成")).toBeTruthy();
    expect(terminal.queryByRole("rowheader", { name: "静态极化率主基组" })).toBeNull();
    expect(monomer.getAllByText("未保存角色适用性；不推定执行")).toHaveLength(3);
    expect(monomer.queryByText("角色适用；阶段已完成，元素分配见日志")).toBeNull();
  });

  it("projects only saved, allowlisted Br/I basis evidence from property conditions", () => {
    const secret = "PRIVATE_BASIS_CANARY";
    const view = page(job({ results: { properties: [
      { key: "qm_polarizability", value: 2.5, unit: "angstrom^3", conditions: { basis_gen: { Br: "6-311G(d,p)", I: "lanl2dz", path: `/private/${secret}`, token: secret } } },
      { key: "qm_homo", value: -7, unit: "eV", conditions: { basis_gen: { Br: `/private/${secret}`, I: { token: secret } } } },
      { key: "refractive_index", value: 1.5, unit: "1", conditions: { qm: { polarizability_angstrom3: 2.5, basis_gen: { I: "lanl2dz" } } } }
    ] } }));
    const evidence = expandEvidence(view.container);
    const entries = evidence.getAllByRole("region", { name: "保存的逐元素基组证据" }).map(element => within(element));
    expect(entries).toHaveLength(3);
    expect(entries[0].getByText("6-311G(d,p)")).toBeTruthy();
    expect(entries[0].getByText("lanl2dz")).toBeTruthy();
    expect(entries[0].getByText(/来自本条性质保存的 basis_gen/)).toBeTruthy();
    expect(entries[1].getByText(/未提供可识别的 Br\/I 逐元素基组记录/)).toBeTruthy();
    expect(entries[2].getByText("lanl2dz")).toBeTruthy();
    expect(view.container.textContent).not.toContain(secret);
    expect(view.container.textContent).not.toContain("当前锁定协议：Br/I 覆盖规则（v9）");
  });

  it("keeps Tg scan conditions, SP source windows and temperature-axis curves traceable", () => {
    const view = page(job({ results: { properties: [
      { key: "tg", value: 350, unit: "K", method: "stepwise_density_bilinear_fit", source_stage: "tg", quality_status: "warning", conditions: { temperature_min_k: 100, temperature_max_k: 800, temperature_interval_k: 10 } },
      { key: "sp_total", value: 18.1, unit: "MPa^0.5", source_stage: "sp", quality_status: "passed", analysis_range: { start_ps: 100, stop_ps: 500 }, conditions: { temperature_k: 300, sampling_ns: .5, burn_in_ps: 100 } },
      { key: "sp_ced", value: 327.61, unit: "J/cm3", source_stage: "sp", quality_status: "passed" }
    ], series: [{ key: "tg_density", label: "Tg 温度—密度曲线", unit: "g/cm3", points: [{ temperature_k: 100, value: 1.1 }, { temperature_k: 800, value: .8 }] }] }, stage_runs: [stage("sp_analyze", { quality: { sample_count: 401, block_count: 5, reasons: ["finite_positive_cohesive_energy"] } }), stage("tg_analyze", { quality_status: "warning", quality: { fit_r2: .99, intersection_k: 350, reasons: ["single_scan_no_independent_seed_validation"] } })] }));
    expect(screen.getByRole("rowheader", { name: "溶解度参数 SP" })).toBeTruthy();
    expect(screen.getByRole("rowheader", { name: "内聚能密度 CED" })).toBeTruthy();
    const evidence = expandEvidence(view.container);
    expect(evidence.getByText("800 K")).toBeTruthy();
    expect(evidence.getByText("10 K")).toBeTruthy();
    expect(evidence.getByText("100–500 ps")).toBeTruthy();
    expect(evidence.getByText("0.5 ns")).toBeTruthy();
    expect(evidence.getByText("350 K")).toBeTruthy();
    expect(evidence.getByText("0.99")).toBeTruthy();
    expect(evidence.getByText(/single_scan_no_independent_seed_validation/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "曲线" }));
    expect(screen.getByText("科学曲线：温度 (K)")).toBeTruthy();
  });
  it("shows reported Tg cooling rate, valid plateau windows and the scope of its time-block uncertainty", () => {
    const conditions = { temperature_min_k: 100, temperature_max_k: 800, temperature_interval_k: 10,
      nominal_cooling_rate_k_per_ns: 500, plateau_duration_ps: 10, temperature_interval_duration_ps: 20,
      uncertainty_model: "five_contiguous_time_blocks_per_temperature;not_independent_seeds" };
    const view = page(job({ results: { properties: [{ key: "tg", value: 350, unit: "K", quality_status: "warning", conditions }] }, stage_runs: [
      stage("tg", { results: { effective_config: conditions } }),
      stage("tg_analyze", { quality_status: "warning", quality: { plateau_analysis_windows: [
        { temperature_k: 800, start_ps: 502.1, stop_ps: 510, sample_count: 80, token: "PRIVATE_PLATEAU_CANARY" },
        { temperature_k: 790, start_ps: 522.1, stop_ps: 530, sample_count: 80 },
        { temperature_k: "PRIVATE_PLATEAU_CANARY", start_ps: 0, stop_ps: 1, sample_count: 10 },
        { temperature_k: 780, start_ps: 20, stop_ps: 10, sample_count: 80 }
      ] } })
    ] }));
    const evidence = expandEvidence(view.container);
    expect(evidence.getAllByText("500 K/ns")).toHaveLength(2);
    expect(evidence.getAllByText("20 ps")).toHaveLength(2);
    expect(evidence.getAllByText(/每个温度平台内分为 5 个连续时间块/)).toHaveLength(2);
    expect(evidence.getAllByText(/不代表独立种子重复，也不包含降温速率敏感性/)).toHaveLength(2);
    const windows = within(evidence.getByRole("region", { name: "Tg 温度平台分析窗口" }));
    expect(windows.getAllByRole("row")).toHaveLength(3);
    expect(windows.getByRole("rowheader", { name: "790" })).toBeTruthy();
    expect(windows.getByText("502.1")).toBeTruthy();
    expect(windows.getByText(/未展示无效条目/)).toBeTruthy();
    expect(view.container.textContent).not.toContain("PRIVATE_PLATEAU_CANARY");
    expect(windows.queryByRole("rowheader", { name: "780" })).toBeNull();
  });

  it("shows the precise SP energy definition, disabled tail correction and TALLY requirement without inferring missing flags", () => {
    const conditions = { temperature_k: 300, n_chain: 10, source_equilibrium_stage: "additional_1", sampling_ns: .5,
      TALLY_required: false, pair_tail_correction: false, decomposition: false,
      cohesive_definition: "minus_inter_molecular_nonbonded_energy_over_volume",
      energy_observation: "pair_inter_plus_long_total_minus_sum_chain_long_self",
      averaging: "mean_of_per_frame_sqrt_CED;matches_native_Solubility_Parameter",
      uncertainty_model: "five_contiguous_time_blocks;not_independent_frames" };
    const view = page(job({ results: { properties: [{ key: "sp_total", value: 18, unit: "MPa^0.5", conditions }] }, stage_runs: [
      stage("sp", { results: { effective_config: conditions } }),
      stage("sp_analyze", { quality: { reasons: [] } })
    ] }));
    const evidence = expandEvidence(view.container);
    const sources = within(evidence.getByRole("region", { name: "性质来源与计算条件" }));
    expect(sources.getByText("内聚能密度 = −分子间非键相互作用能 / 体积")).toBeTruthy();
    expect(sources.getByText("分子间成对非键能 + 总长程能 − 各链长程自能之和")).toBeTruthy();
    expect(sources.getByText("每帧内聚能密度开平方后取平均（原生 SP 定义）")).toBeTruthy();
    expect(sources.getByText(/时间相关帧不作为独立样本，也不包含独立种子重复的误差/)).toBeTruthy();
    expect(sources.getByText("第 1 次追加平衡")).toBeTruthy();
    for (const label of ["需要 LAMMPS TALLY", "成对相互作用尾修正", "能量分解"]) {
      expect(within(sources.getByText(label).closest("div")!).getByText("否")).toBeTruthy();
    }
    const runtime = within(evidence.getByRole("region", { name: "阶段协议与运行时来源" }));
    expect(runtime.getByText("内聚能密度 = −分子间非键相互作用能 / 体积")).toBeTruthy();
    expect(runtime.queryByText("热流分解")).toBeNull();
    cleanup();
    const absent = page(job({ results: { properties: [{ key: "sp_ced", value: 324, unit: "J/cm3", conditions: { TALLY_required: "false", pair_tail_correction: null } }] } }));
    const absentEvidence = expandEvidence(absent.container);
    expect(absentEvidence.queryByText("需要 LAMMPS TALLY")).toBeNull();
    expect(absentEvidence.queryByText("成对相互作用尾修正")).toBeNull();
  });

  it("shows each blend chain species native tacticity without inventing stereocenter applicability", () => {
    const view = page(job({ scientific_request: { ...POLYMER_MD_DEFAULTS, n_chain: 4,
      model_configuration: { version: 1, mode: "blend", components: [
        { id: "A", smiles: "*CC*", n_chain: 2, chain_length: { mode: "dp", value: 20 } },
        { id: "B", smiles: "*CCO*", n_chain: 2, chain_length: { mode: "molecular_weight", value: 6400 } }
      ] } }, stage_runs: [stage("build", { results: { chain_species: [
        { n_chain: 2, actual_dp: 20, sequence: ["A"], actual_tacticity: "isotactic" },
        { n_chain: 2, actual_dp: 20, sequence: ["B"], actual_tacticity: "none" }
      ] } })] }));
    const requestSummary = screen.getByRole("region", { name: "任务请求摘要" });
    expect(within(requestSummary).getByLabelText("组分 A SMILES").textContent).toBe("*CC*");
    expect(within(requestSummary).getByLabelText("组分 B SMILES").textContent).toBe("*CCO*");
    expect(requestSummary.textContent).toContain("请求 2 条链 · 每链目标聚合度 DP 20");
    expect(requestSummary.textContent).toContain("请求 2 条链 · 每链目标分子量 6,400 g/mol");
    expect(requestSummary.textContent).not.toContain("每链目标 1,000 个原子");
    const evidence = expandEvidence(view.container);
    const composition = within(evidence.getByRole("region", { name: "实际组成与分子量统计" }));
    expect(within(composition.getByText("链种 1 的实际构建").closest("details")!).getByText("全同")).toBeTruthy();
    expect(within(composition.getByText("链种 2 的实际构建").closest("details")!).getByText("未识别出立构类型")).toBeTruthy();
    expect(composition.getByText(/当前记录未提供适用中心计数，不能仅据分类判定立构控制是否适用/)).toBeTruthy();
    expect(composition.queryByText("无适用立构中心")).toBeNull();
  });

  it("keeps requested parameters separate from the measured model, stage runtime and property conditions", () => {
    const view = page(job({ results: { properties: [{ key: "density", value: 0.91, unit: "g/cm3", method: "production_mean_with_5_block_SE", quality_status: "passed", uncertainty: 0.001, source_stage: "eq3", analysis_range: windowData, conditions: { temperature_k: 315, pressure_atm: 2, ensemble: "NPT" } }] }, stage_runs: [stage("build", { results: { actual_dp: 166, atoms_per_chain: 1004, actual_atom_count: 10040, n_chain: 10, actual_tacticity: "atactic", runtime_fingerprint: fingerprint } }), stage("eq3", { results: { runtime_fingerprint: fingerprint, effective_config: { thermo_steps: 1000, input_sha256: "c".repeat(64) } } })] }));
    const evidence = expandEvidence(view.container);
    const request = evidence.getByRole("region", { name: "持久化科学请求" });
    expect(within(request).getByText("300 K")).toBeTruthy();
    expect(within(request).getByText("1,000")).toBeTruthy();
    const model = evidence.getByRole("region", { name: "实际构建模型" });
    expect(within(model).getByText("1,004")).toBeTruthy();
    expect(within(model).getByText("10,040")).toBeTruthy();
    expect(within(model).getByText("166")).toBeTruthy();
    expect(evidence.getAllByText(fingerprint)).toHaveLength(2);
    expect(evidence.queryByText(currentFingerprint)).toBeNull();
    const property = evidence.getByRole("region", { name: "性质来源与计算条件" });
    expect(within(property).getByText("315 K")).toBeTruthy();
    expect(within(property).getByText("2 atm")).toBeTruthy();
    expect(within(property).getByText("production_mean_with_5_block_SE")).toBeTruthy();
    expect(within(property).getByText("2,100–5,000 ps")).toBeTruthy();
    expect(within(property).getByText("2,901")).toBeTruthy();
    expect(within(property).getByText("0 ps")).toBeTruthy();
  });

  it("renders both RNEMD gradient fits, exchange slope, fit ranges and the reported quality reasons", () => {
    const fit = { slope_k_m: -2.4e8, intercept_k: 320, r2: 0.991, slope_standard_error_k_m: 1.5e6, fit_window: { start_m: 1e-9, stop_m: 9e-9, sample_count: 9 }, profile_time_window: { start_ps: 800.2, stop_ps: 1000, sample_count: 999 } };
    const view = page(job({ scientific_request: { ...POLYMER_MD_DEFAULTS, calculation: "basic_tc", seed: 1729 }, stage: "tc_analyze", quality_status: "not_converged", stage_runs: [stage("tc_analyze", { quality_status: "not_converged", quality: { left_r2: 0.991, right_r2: 0.987, qualified_block_rate: 0.7, qualified_profile_blocks: 700, production_profile_blocks: 1000, analysis_range: windowData, temperature_gradient_fits: { left: fit, right: { ...fit, slope_k_m: 2.2e8, r2: 0.987 } }, exchange_energy_fit: { slope_w_m2: 1.8e8, r2: 0.999, slope_standard_error_w_m2: 2000, fit_window: windowData }, conductivity_block_estimates: [0.7, 0.8, 1.1, 1.2, 1.5], reasons: ["conductivity_block_uncertainty_not_converged"] } })] }));
    const evidence = expandEvidence(view.container);
    const left = evidence.getByRole("region", { name: "左侧温度梯度拟合" });
    expect(within(left).getByText("-2.4000e+8 K/m")).toBeTruthy();
    expect(within(left).getByText("0.991")).toBeTruthy();
    expect(within(left).getByText("1.0000e-9 m")).toBeTruthy();
    expect(within(left).getByText("800.2–1,000 ps")).toBeTruthy();
    const right = evidence.getByRole("region", { name: "右侧温度梯度拟合" });
    expect(within(right).getByText("2.2000e+8 K/m")).toBeTruthy();
    const exchange = evidence.getByRole("region", { name: "交换能量拟合" });
    expect(within(exchange).getByText("1.8000e+8 W/m²")).toBeTruthy();
    expect(within(exchange).getByText("2,000 W/m²")).toBeTruthy();
    expect(evidence.getByText("0.7")).toBeTruthy();
    expect(evidence.getByText(/服务端质量原因：conductivity_block_uncertainty_not_converged/)).toBeTruthy();
  });

  it("shows the requested burn-in and available observations when no valid analysis window exists", () => {
    const empty = { start_ps: null, stop_ps: null, sample_count: 0, requested_start_ps: 800, observed_start_ps: 0, observed_stop_ps: 1 };
    const view = page(job({ scientific_request: { ...POLYMER_MD_DEFAULTS, calculation: "basic_tc" }, quality_status: "not_converged", results: { properties: [{ key: "thermal_conductivity", value: null, unit: "W/(m K)", quality_status: "failed", source_stage: "tc", analysis_range: empty, conditions: { axis: "x", time_step_fs: 0.2, burn_in_ps: 800, decomp: false } }] }, stage_runs: [stage("tc_analyze", { quality_status: "not_converged", quality: { analysis_range: empty, production_profile_blocks: 0, reasons: ["insufficient_RNEMD_steady_state_samples"] } })] }));
    const evidence = expandEvidence(view.container);
    expect(evidence.getAllByText("未提供有效分析时间窗").length).toBeGreaterThan(0);
    expect(evidence.getAllByText("800 ps").length).toBeGreaterThan(0);
    expect(evidence.getAllByText("1 ps").length).toBeGreaterThan(0);
    expect(evidence.getAllByText("未提供该侧温度梯度拟合。")).toHaveLength(2);
    expect(screen.getByText("不可用")).toBeTruthy();
    expect(evidence.queryByText(/0–800 ps/)).toBeNull();
  });

  it("retains basic properties when TC failed, and does not invent RNEMD fit evidence", () => {
    const view = page(job({ status: "failed", quality_status: "failed", scientific_request: { ...POLYMER_MD_DEFAULTS, calculation: "basic_tc" }, results: { properties: [{ key: "density", value: 0.923, unit: "g/cm3", quality_status: "passed", source_stage: "eq3", analysis_range: windowData }] }, stage_runs: [stage("analyze_0"), stage("tc", { status: "failed", quality_status: "failed" })] }));
    const evidence = expandEvidence(view.container);
    expect(screen.getByText("0.923")).toBeTruthy();
    expect(evidence.getByText(/尚无 RNEMD 分析阶段质量证据/)).toBeTruthy();
    expect(evidence.queryByRole("region", { name: "交换能量拟合" })).toBeNull();
  });

  it("keeps historical scalar results readable and explicitly marks absent provenance", () => {
    const view = page(job({ results: { density: 0.88 }, stage_runs: [stage("eq3")] }));
    const evidence = expandEvidence(view.container);
    expect(screen.getByText("0.88")).toBeTruthy();
    expect(evidence.getByText(/请求原子数不等于实际原子数/)).toBeTruthy();
    expect(evidence.getByText("此阶段未提供运行时指纹")).toBeTruthy();
    expect(evidence.getByText("未提供来源阶段")).toBeTruthy();
    expect(evidence.getByText("未提供可展示的性质计算条件。")).toBeTruthy();
    expect(evidence.queryByText(currentFingerprint)).toBeNull();
  });

  it("shows derived dependency conditions, source windows and missing propagated uncertainty", () => {
    const view = page(job({ results: { properties: [{ key: "thermal_diffusivity", value: null, unit: "m2/s", method: "kappa_over_density_Cp", source_stage: "tc", quality_status: "failed", conditions: { dependencies: { density: { value: 0.9, unit: "g/cm3", quality_status: "passed", source_stage: "additional_1", analysis_range: windowData, conditions: { temperature_k: 300, ensemble: "NPT" } }, thermal_conductivity: { value: 0.2, unit: "W/(m K)", quality_status: "warning", source_stage: "tc", analysis_range: { start_ps: 800, stop_ps: 1000 } } }, uncertainty_model: "not_propagated_from_kappa_density_Cp;cross_stage_covariances_not_estimated" } }] } }));
    const evidence = expandEvidence(view.container);
    const dep = evidence.getByRole("region", { name: "密度依赖来源" });
    expect(within(dep).getByText("第 1 次追加平衡")).toBeTruthy();
    expect(within(dep).getByText("2,100–5,000 ps")).toBeTruthy();
    expect(within(dep).getByText("g/cm3")).toBeTruthy();
    expect(evidence.getByText(/未传播 κ、密度和 Cp 的全部误差/)).toBeTruthy();
    expect(evidence.getByText("未提供误差估计")).toBeTruthy();
  });

  it("projects known scientific keys and excludes private metadata even when nested under evidence objects", () => {
    const secret = "PRIVATE_CANARY_DO_NOT_RENDER";
    const view = page(job({ scientific_request: { ...POLYMER_MD_DEFAULTS, calculation: "basic_tc" }, results: { properties: [{ key: "density", value: 0.9, unit: "g/cm3", conditions: { temperature_k: 300, path: `/private/${secret}`, pid: 123456789, token: secret, qm: { polarizability_angstrom3: 2, polarizability_method: `file:///${secret}`, password: secret }, dependencies: { secret: { value: secret } } }, analysis_range: { ...windowData, storage_ref: secret } }] }, stage_runs: [stage("build", { results: { actual_atom_count: 10040, runtime_fingerprint: `/private/${secret}`, effective_config: { time_step_fs: 1, input_sha256: `/private/${secret}`, token: secret }, patch_version: `token=${secret}`, work_dir: secret, pid: 123456789 } }), stage("tc_analyze", { quality: { left_r2: 0.99, path: secret, reasons: [`/private/${secret}`, `password=${secret}`], temperature_gradient_fits: { left: { slope_k_m: 2e8, pid: 123456789, raw: { token: secret } } } } })] }));
    const evidence = expandEvidence(view.container);
    expect(evidence.getByText("10,040")).toBeTruthy();
    expect(evidence.getByText("0.99")).toBeTruthy();
    expect(view.container.textContent).not.toContain(secret);
    expect(view.container.textContent).not.toContain("123456789");
    expect(evidence.getAllByText("此阶段未提供运行时指纹").length).toBeGreaterThan(0);
  });

  it("renders basic stability limits and trajectory gates from results quality while preserving earlier stage windows", () => {
    const view = page(job({ results: { properties: [{ key: "density", value: 0.92, unit: "g/cm3", source_stage: "additional_1", analysis_range: { start_ps: 2500, stop_ps: 5000 } }] }, stage_runs: [stage("analyze_0", { quality: { internal: "ignored" }, results: { properties: [{ key: "density", value: 0.89, unit: "g/cm3", source_stage: "eq3", analysis_range: windowData }], quality: { checks: { sample_count: 2901, finite: true, Density: { block_mean_sd: 0.002, limit: 0.001, passed: false } }, trajectory: { structure_equilibrated: false, production_frame_count: 200, nematic_order_below_0_1: true } } } })] }));
    const evidence = expandEvidence(view.container);
    expect(evidence.getByRole("heading", { name: "基础物性分析 · analyze_0-1" })).toBeTruthy();
    expect(evidence.getByText("2,100–5,000 ps")).toBeTruthy();
    expect(evidence.getByText("2,500–5,000 ps")).toBeTruthy();
    const gates = evidence.getByRole("region", { name: "基础平衡判据" });
    expect(within(gates).getByText("0.002")).toBeTruthy();
    expect(within(gates).getByText("0.001")).toBeTruthy();
    expect(within(gates).getByText("否")).toBeTruthy();
    expect(evidence.getByRole("region", { name: "构象与取向判据" })).toBeTruthy();
  });

  it("keeps historical quality summaries without claiming an absent analysis stage exists", () => {
    const view = page(job({ scientific_request: { ...POLYMER_MD_DEFAULTS, calculation: "basic_tc" }, results: { quality_summary: { thermal: { stage_run_id: "tc_analyze-2", quality_status: "not_converged", left_r2: 0.8, qualified_block_rate: 0.4, reasons: ["temperature_gradient_not_stable_or_linear"] } } } }));
    const evidence = expandEvidence(view.container);
    expect(evidence.getByText(/仅保留汇总质量，缺少对应分析阶段记录/)).toBeTruthy();
    expect(evidence.getByText("tc_analyze-2")).toBeTruthy();
    expect(evidence.getByText("0.8")).toBeTruthy();
    expect(evidence.getByText("0.4")).toBeTruthy();
    expect(evidence.queryByText(/尚无 RNEMD 分析阶段质量证据/)).toBeNull();
  });

  it("renders actual sampled distance coverage and retires it when the trajectory stage changes", async () => {
    api.trajectory.mockImplementation(async (id, stageId) => ({ job_id: id, stage_run_id: stageId, total_frames: 20001, frames: [{ frame_index: 0, time_ps: 0, points: [{ atom_id: 1, chain_id: 1, atom_type: "C", x: 0, y: 0, z: 0 }], box: { lx: 10, ly: 10, lz: 10 } }] }));
    api.distance.mockImplementation(async (id, stageId) => ({ job_id: id, stage_run_id: stageId, frames: [0, 3], time_ps: [0, 3], distance: [1, 1.1], sampling: { source_frame_count: 20001, start_frame: 0, stop_frame: 20001, stride: 3, sampled_frames: 2, first_frame: 0, last_frame: 3, start_ps: 0, stop_ps: 3 } }));
    page(job({ stage_runs: [stage("eq3"), stage("additional_1")] }), { ...capabilities, trajectory_available: true, distance_available: true });
    fireEvent.click(screen.getByRole("tab", { name: "构象" }));
    await screen.findByRole("button", { name: "选取原子对" });
    fireEvent.click(screen.getByRole("button", { name: "选取原子对" }));
    fireEvent.click(screen.getByRole("button", { name: "读取距离" }));
    await screen.findByText("距离分析已返回");
    expect(api.distance.mock.calls[0][2]).toEqual({ atom_id_1: 1, atom_id_2: 2, use_pbc: true, frame_count: 20001 });
    expect(screen.getByLabelText("原子距离采样范围").textContent).toContain("源轨迹 20,001 帧");
    expect(screen.getByLabelText("原子距离采样范围").textContent).toContain("步长 3");
    expect(screen.getByLabelText("原子距离采样范围").textContent).toContain("实际 2 个采样点（第 1–4 帧），时间 0–3 ps");
    fireEvent.click(screen.getByRole("combobox", { name: "轨迹阶段" }));
    fireEvent.click(within(screen.getByRole("listbox", { name: "轨迹阶段" })).getByText("第 1 次追加平衡 · 第 1 次", { selector: "strong", exact: true }).closest('[role="option"]')!);
    await waitFor(() => expect(api.trajectory).toHaveBeenLastCalledWith(expect.any(String), "additional_1-1", 0, expect.anything()));
    expect(screen.queryByLabelText("原子距离采样范围")).toBeNull();
  });

  it("keeps stages and scientific evidence in overview while all four result tabs support keyboard navigation", () => {
    page(job({ stage_runs: [stage("eq3")], results: { density: .91 } }));
    const tabs = within(screen.getByRole("tablist", { name: "聚合物 MD 结果视图" }));
    expect(tabs.getAllByRole("tab")).toHaveLength(4);
    const overview = tabs.getByRole("tab", { name: "概览" });
    expect(overview.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel", { name: "概览" }).getAttribute("id")).toBe(overview.getAttribute("aria-controls"));
    expect(screen.getByRole("region", { name: "阶段记录" })).toBeTruthy();
    expect(screen.getByText("运行条件与质量证据")).toBeTruthy();
    expect(screen.getByText("0.91")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "计算产物" })).toBeNull();

    overview.focus();
    fireEvent.keyDown(overview, { key: "ArrowRight" });
    expect(document.activeElement).toBe(tabs.getByRole("tab", { name: "曲线" }));
    expect(screen.getByRole("tabpanel", { name: "曲线" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "尚无可用曲线" })).toBeTruthy();
    expect(screen.queryByRole("region", { name: "阶段记录" })).toBeNull();
    expect(screen.queryByText("运行条件与质量证据")).toBeNull();
    expect(api.trajectory).not.toHaveBeenCalled();

    fireEvent.keyDown(document.activeElement!, { key: "End" });
    expect(document.activeElement).toBe(tabs.getByRole("tab", { name: "输出文件" }));
    expect(screen.getByRole("tabpanel", { name: "输出文件" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "暂无可下载文件" })).toBeTruthy();
    expect((screen.getByRole("button", { name: "清理输出文件" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("region", { name: "阶段记录" })).toBeNull();

    fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
    expect(document.activeElement).toBe(overview);
    fireEvent.keyDown(overview, { key: "ArrowLeft" });
    expect(document.activeElement).toBe(tabs.getByRole("tab", { name: "输出文件" }));
    fireEvent.keyDown(document.activeElement!, { key: "Home" });
    expect(document.activeElement).toBe(overview);
    expect(screen.getByText("0.91")).toBeTruthy();
    expect(screen.getByText("运行条件与质量证据")).toBeTruthy();
  });

  it("shows every published job and stage curve on its actual axis without mounting stage logs", () => {
    page(job({ results: { series: [{ key: "density", label: "生产段密度", unit: "g/cm3", points: [{ time_ps: 0, value: .9 }, { time_ps: 1, value: .91 }] }] }, stage_runs: [
      stage("tc", { results: { series: [{ key: "temperature_profile", label: "Temperature profile", unit: "K", points: [{ position_angstrom: 0, value: 290 }, { position_angstrom: 10, value: 310 }] }] } }),
      stage("tg", { results: { series: [{ key: "tg_density", label: "Tg 温度—密度曲线", unit: "g/cm3", points: [{ temperature_k: 100, value: 1.1 }, { temperature_k: 800, value: .8 }] }] } })
    ] }));
    fireEvent.click(screen.getByRole("tab", { name: "曲线" }));
    expect(screen.getByRole("heading", { name: "生产段密度" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "稳态温度分布" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Tg 温度—密度曲线" })).toBeTruthy();
    expect(screen.getByText("科学曲线：位置 (Å)")).toBeTruthy();
    expect(screen.getByText("科学曲线：温度 (K)")).toBeTruthy();
    expect(screen.getByText("科学曲线")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "阶段记录" })).toBeNull();
    expect(screen.queryByText("运行条件与质量证据")).toBeNull();
    expect(api.logs).not.toHaveBeenCalled();
  });

  it("keeps loaded coordinates, atoms and a successful distance through analysis unavailability and freshness recovery", async () => {
    api.trajectory.mockImplementation(async (id, stageId, offset) => ({ job_id: id, stage_run_id: stageId, total_frames: 20001, frames: [{ frame_index: offset, time_ps: 0,
      points: [{ atom_id: 320, chain_id: 1, atom_type: "C", x: 0, y: 0, z: 0 }, { atom_id: 1253, chain_id: 2, atom_type: "C", x: 1, y: 0, z: 0 }] }] }));
    api.distance.mockImplementation(async (id, stageId) => ({ job_id: id, stage_run_id: stageId, frames: [0], time_ps: [0], distance: [1] }));
    const value = job({ stage_runs: [stage("eq3")] });
    const ready = { ...capabilities, trajectory_available: true, distance_available: true, analysis: { status: "ready" as const, timeout_seconds: 90 } };
    const props = { job: value, busy: null, onCancel: vi.fn(), onClear: vi.fn(), onDelete: vi.fn() };
    const view = render(<PolymerMdResults {...props} capabilities={ready} serviceCheck="confirmed" />);
    fireEvent.click(screen.getByRole("tab", { name: "构象" })); await screen.findByRole("button", { name: "选择320与1253" });
    fireEvent.click(screen.getByRole("button", { name: "选择320与1253" })); fireEvent.click(screen.getByRole("button", { name: "读取距离" }));
    await screen.findByText("距离分析已返回");
    expect(api.distance.mock.calls[0][2]).toEqual({ atom_id_1: 320, atom_id_2: 1253, frame_count: 20001, use_pbc: true });
    view.rerender(<PolymerMdResults {...props} capabilities={{ ...ready, trajectory_available: false, distance_available: false, analysis: { status: "unavailable", timeout_seconds: 90 } }} serviceCheck="confirmed" />);
    expect(screen.getByLabelText("已选原子").textContent).toBe("320/1253");
    expect(screen.getByText("距离分析已返回")).toBeTruthy();
    expect((screen.getByRole("button", { name: "读取距离" }) as HTMLButtonElement).disabled).toBe(true);
    view.rerender(<PolymerMdResults {...props} capabilities={ready} serviceCheck="unconfirmed" />);
    expect(screen.getByText(/服务状态待确认，已保留/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "读取距离" }) as HTMLButtonElement).disabled).toBe(true);
    view.rerender(<PolymerMdResults {...props} capabilities={{ ...ready, analysis: { status: "busy", timeout_seconds: 90 } }} serviceCheck="confirmed" />);
    expect((screen.getByRole("button", { name: "读取距离" }) as HTMLButtonElement).disabled).toBe(false);
    expect(api.trajectory).toHaveBeenCalledOnce(); expect(api.distance).toHaveBeenCalledOnce();
    api.distance.mockRejectedValueOnce(Object.assign(new Error("worker wording"), { code: "analysis_timeout" }));
    fireEvent.click(screen.getByRole("button", { name: "读取距离" })); await screen.findByRole("alert");
    expect(screen.getByRole("alert").textContent).toContain("距离分析超过服务处理时限");
    expect(screen.getByText("距离分析已返回")).toBeTruthy();
    expect(screen.getByLabelText("已选原子").textContent).toBe("320/1253");
  });

  it("locks distance submissions, stops only waiting and bounds hung analysis to 100 seconds", async () => {
    vi.useFakeTimers();
    try {
      api.trajectory.mockImplementation(async (id, stageId) => ({ job_id: id, stage_run_id: stageId, total_frames: 1, frames: [{ frame_index: 0, time_ps: 0,
        points: [{ atom_id: 1, chain_id: 1, atom_type: "C", x: 0, y: 0, z: 0 }] }] }));
      let resolveOld!: (value: unknown) => void;
      api.distance.mockImplementationOnce(() => new Promise(done => { resolveOld = done; })).mockImplementationOnce(() => new Promise(() => {}));
      const cancel = vi.fn();
      const value = job({ stage_runs: [stage("eq3")] });
      render(<PolymerMdResults job={value} capabilities={{ ...capabilities, trajectory_available: true, distance_available: true }} busy={null} onCancel={cancel} onClear={vi.fn()} onDelete={vi.fn()} />);
      fireEvent.click(screen.getByRole("tab", { name: "构象" })); await act(async () => {});
      fireEvent.click(screen.getByRole("button", { name: "选取原子对" }));
      act(() => { fireEvent.click(screen.getByRole("button", { name: "读取距离" })); fireEvent.click(screen.getByRole("button", { name: "读取距离" })); });
      expect(api.distance).toHaveBeenCalledOnce();
      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(screen.getByText("已等待 3 秒")).toBeTruthy();
      const oldSignal = api.distance.mock.calls[0][3] as AbortSignal;
      fireEvent.click(screen.getByRole("button", { name: "停止等待分析" }));
      expect(oldSignal.aborted).toBe(true); expect(cancel).not.toHaveBeenCalled();
      expect(screen.getByLabelText("已选原子").textContent).toBe("1/2");
      expect(screen.getByText(/后台可能仍在处理/)).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "读取距离" }));
      await act(async () => resolveOld({ job_id: value.job_id, stage_run_id: "eq3-1", frames: [0], time_ps: [0], distance: [1] }));
      expect(screen.queryByText("距离分析已返回")).toBeNull();
      await act(() => vi.advanceTimersByTimeAsync(100000));
      expect((api.distance.mock.calls[1][3] as AbortSignal).aborted).toBe(true);
      expect(screen.getByRole("alert").textContent).toContain("距离分析等待已超时");
      expect((screen.getByRole("button", { name: "读取距离" }) as HTMLButtonElement).disabled).toBe(false);
      expect(screen.getByLabelText("已选原子").textContent).toBe("1/2");
      expect(api.distance).toHaveBeenCalledTimes(2);
    } finally { cleanup(); vi.useRealTimers(); api.distance.mockReset(); }
  });

  it("clears an earlier trajectory read error when independent analysis readiness recovers", async () => {
    const value = job({ stage_runs: [stage("eq3")] });
    api.trajectory.mockRejectedValueOnce(new Error("初次轨迹读取失败")).mockResolvedValueOnce({ job_id: value.job_id, stage_run_id: "eq3-1", total_frames: 1,
      frames: [{ frame_index: 0, time_ps: 0, points: [{ atom_id: 1, chain_id: 1, atom_type: "C", x: 0, y: 0, z: 0 }] }] });
    const ready = { ...capabilities, trajectory_available: true, distance_available: true };
    const props = { job: value, busy: null, onCancel: vi.fn(), onClear: vi.fn(), onDelete: vi.fn() };
    const view = render(<PolymerMdResults {...props} capabilities={ready} serviceCheck="confirmed" />);
    fireEvent.click(screen.getByRole("tab", { name: "构象" }));
    await screen.findByText("初次轨迹读取失败");
    view.rerender(<PolymerMdResults {...props} capabilities={{ ...ready, analysis: { status: "unavailable", timeout_seconds: 90 } }} serviceCheck="confirmed" />);
    view.rerender(<PolymerMdResults {...props} capabilities={ready} serviceCheck="confirmed" />);
    await screen.findByRole("button", { name: "选取原子对" });
    expect(screen.queryByText("初次轨迹读取失败")).toBeNull();
    expect(api.trajectory).toHaveBeenCalledTimes(2);
  });

  it("keeps output actions bound to the selected job and retains scientific properties after file cleanup", async () => {
    const value = job({ results: { density: .923 }, artifacts: [{ artifact_id: "density", stage_run_id: "eq3-1", filename: "density.csv", kind: "results", media_type: "text/csv", size_bytes: 200, sha256: "a".repeat(64) }] });
    const cancel = vi.fn(), clear = vi.fn(), remove = vi.fn();
    const view = render(<PolymerMdResults job={value} capabilities={capabilities} busy={null} onCancel={cancel} onClear={clear} onDelete={remove} />);
    expect(screen.queryByRole("button", { name: "下载 density.csv" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "输出文件" }));
    fireEvent.click(screen.getByRole("button", { name: "下载 density.csv" }));
    await waitFor(() => expect(api.download).toHaveBeenCalledWith(value.job_id, "density", "density.csv", 200, expect.any(AbortSignal)));
    expect(clear).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "清理输出文件" }));
    expect(clear).toHaveBeenCalledOnce();
    expect(remove).not.toHaveBeenCalled();
    expect(cancel).not.toHaveBeenCalled();
    view.rerender(<PolymerMdResults job={{ ...value, artifact_state: "deleted" }} capabilities={capabilities} busy={null} onCancel={cancel} onClear={clear} onDelete={remove} />);
    expect(screen.queryByRole("button", { name: "下载 density.csv" })).toBeNull();
    expect((screen.getByRole("button", { name: "清理输出文件" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "下载所有产物" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("tab", { name: "构象" }));
    expect(screen.getByText(/当前服务尚未提供轨迹浏览/)).toBeTruthy();
    expect(api.trajectory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: "概览" }));
    expect(screen.getByText("0.923")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "删除任务记录" }));
    expect(remove).toHaveBeenCalledOnce();
  });

  it("distinguishes same-named artifacts by saved stages while preserving every ID in individual and bundle downloads", async () => {
    const stages = [stage("eq1"), stage("eq3", { stage_run_id: "eq3-2", attempt: 2 })];
    const files = ["eq1-1", "eq3-2", "unknown-stage"].map((stageId, index) => ({ artifact_id: `trajectory-${index}`, stage_run_id: stageId,
      filename: "trajectory.xtc", kind: "trajectory", media_type: "application/octet-stream", size_bytes: 100 + index, sha256: "a".repeat(64) }));
    const value = job({ stage_runs: stages, artifacts: files });
    page(value);
    fireEvent.click(screen.getByRole("tab", { name: "输出文件" }));
    const buttons = screen.getAllByRole("button", { name: "下载 trajectory.xtc" });
    expect(buttons).toHaveLength(3);
    const origins = buttons.map(button => document.getElementById(button.getAttribute("aria-describedby")!)?.textContent);
    expect(origins).toEqual(["来源：EQ1 松弛与装填 · 第 1 次", "来源：EQ3 平衡采样 · 第 2 次", "来源：阶段来源未记录"]);
    for (const [index, button] of buttons.entries()) {
      fireEvent.click(button);
      await waitFor(() => expect(api.download).toHaveBeenLastCalledWith(value.job_id, files[index].artifact_id, "trajectory.xtc", files[index].size_bytes, expect.any(AbortSignal)));
      await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    }
    expect(api.download).toHaveBeenCalledTimes(3);
    api.bundle.mockResolvedValueOnce(undefined);
    fireEvent.click(screen.getByRole("button", { name: "下载所有产物" }));
    await waitFor(() => expect(api.bundle).toHaveBeenCalledWith(value.job_id, files, expect.any(Function), expect.any(AbortSignal)));
    expect(api.bundle.mock.calls[0][1].map((file: { artifact_id: string }) => file.artifact_id)).toEqual(files.map(file => file.artifact_id));
  });

  it("shows loading inside the trajectory region until the requested frame is ready", async () => {
    let resolve!: (value: unknown) => void;
    api.trajectory.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const value = job({ stage_runs: [stage("eq3")] });
    page(value, { ...capabilities, trajectory_available: true });
    expect(api.trajectory).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: "构象" }));
    const trajectory = within(screen.getByRole("region", { name: "正式任务轨迹" }));
    expect(trajectory.getByRole("status").textContent).toContain("正在读取当前任务的轨迹…");
    expect((trajectory.getByRole("button", { name: "下一帧" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "选取原子对" })).toBeNull();
    await act(async () => resolve({ job_id: value.job_id, stage_run_id: "eq3-1", total_frames: 1,
      frames: [{ frame_index: 0, time_ps: 2, points: [{ atom_id: 1, chain_id: 1, atom_type: "C", x: 0, y: 0, z: 0 }] }] }));
    expect(trajectory.queryByText("正在读取当前任务的轨迹…")).toBeNull();
    expect(trajectory.getByRole("button", { name: "选取原子对" })).toBeTruthy();
    expect(trajectory.getByText((_, element) => element?.tagName === "P" && element.textContent === "采样坐标：1 个原子 · 2 ps")).toBeTruthy();
  });

  it("saves a fixed current-artifact batch, locks competing actions and cancels only the download", async () => {
    const files = ["density.csv", "equilibrium.log"].map((filename, index) => ({ artifact_id: `file-${index}`, stage_run_id: "eq3-1", filename, kind: "results", media_type: "text/plain", size_bytes: 200, sha256: "a".repeat(64) }));
    const value = job({ artifacts: files });
    let progress!: (done: number, total: number) => void, reject!: (error: Error) => void;
    api.bundle.mockImplementation((_id, _files, update) => { progress = update; return new Promise((_done, fail) => { reject = fail; }); });
    const cancel = vi.fn(), clear = vi.fn(), remove = vi.fn();
    const view = render(<PolymerMdResults job={value} capabilities={capabilities} busy={null} onCancel={cancel} onClear={clear} onDelete={remove} />);
    fireEvent.click(screen.getByRole("tab", { name: "输出文件" }));
    const all = screen.getByRole("button", { name: "下载所有产物" });
    fireEvent.click(all); fireEvent.click(all);
    expect(api.bundle).toHaveBeenCalledOnce();
    const [id, snapshot, , signal] = api.bundle.mock.calls[0];
    expect(id).toBe(value.job_id);
    expect(snapshot).toEqual(files);
    expect(snapshot).not.toBe(files);
    expect(snapshot[0]).not.toBe(files[0]);
    for (const name of ["下载 density.csv", "清理输出文件", "删除任务记录"]) {
      const button = screen.getByRole("button", { name }) as HTMLButtonElement;
      expect(button.disabled).toBe(true); fireEvent.click(button);
    }
    expect(api.download).not.toHaveBeenCalled();
    expect(clear).not.toHaveBeenCalled(); expect(remove).not.toHaveBeenCalled();
    view.rerender(<PolymerMdResults job={{ ...value, artifacts: [...files, { ...files[0], artifact_id: "new-file", filename: "later.csv" }] }} capabilities={capabilities} busy={null} onCancel={cancel} onClear={clear} onDelete={remove} />);
    expect(snapshot).toHaveLength(2);
    await act(async () => progress(1, 2));
    expect(screen.getByRole("status", { name: "产物下载进度" }).textContent).toContain("已接收 1 / 2 个产物");
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
    expect((signal as AbortSignal).aborted).toBe(true);
    await act(async () => { progress(2, 2); reject(new Error("迟到下载错误")); });
    expect(screen.queryByRole("status", { name: "产物下载进度" })).toBeNull();
    expect(screen.queryByText("迟到下载错误")).toBeNull();
    expect((all as HTMLButtonElement).disabled).toBe(false);
    let finishSingle!: () => void;
    api.download.mockImplementationOnce(() => new Promise<void>(done => { finishSingle = done; }));
    fireEvent.click(screen.getByRole("button", { name: "下载 density.csv" }));
    expect((all as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(all);
    expect(api.bundle).toHaveBeenCalledOnce();
    const singleSignal = api.download.mock.calls[0][4] as AbortSignal;
    fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
    expect(singleSignal.aborted).toBe(true);
    await act(async () => finishSingle());
    expect(cancel).not.toHaveBeenCalled();
  });

  it.each(["job", "session", "unmount"] as const)("aborts bundle downloads on %s changes and rejects late state writes", async change => {
    const artifact = { artifact_id: "file", stage_run_id: "eq3-1", filename: "density.csv", kind: "results", media_type: "text/csv", size_bytes: 200, sha256: "a".repeat(64) };
    const value = job({ artifacts: [artifact], status: "running" });
    let progress!: (done: number, total: number) => void, reject!: (error: Error) => void;
    api.bundle.mockImplementation((_id, _files, update) => { progress = update; return new Promise((_done, fail) => { reject = fail; }); });
    const view = page(value);
    fireEvent.click(screen.getByRole("tab", { name: "输出文件" }));
    expect(screen.getByText(/任务仍在运行，本次下载/).textContent).toContain("当前已生成的 1 个产物");
    fireEvent.click(screen.getByRole("button", { name: "下载所有产物" }));
    const signal = api.bundle.mock.calls[0][3] as AbortSignal;
    if (change === "job") view.rerender(<PolymerMdResults job={{ ...value, job_id: "another-job" }} capabilities={capabilities} busy={null} onCancel={vi.fn()} onClear={vi.fn()} onDelete={vi.fn()} />);
    else if (change === "session") act(() => retireSession());
    else view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { progress(1, 1); reject(new Error("旧任务下载错误")); });
    expect(screen.queryByRole("status", { name: "产物下载进度" })).toBeNull();
    expect(screen.queryByText("旧任务下载错误")).toBeNull();
    if (change === "job") {
      api.bundle.mockResolvedValueOnce(undefined);
      fireEvent.click(screen.getByRole("button", { name: "下载所有产物" }));
      await waitFor(() => expect(api.bundle).toHaveBeenLastCalledWith("another-job", [artifact], expect.any(Function), expect.any(AbortSignal)));
    }
  });
});
