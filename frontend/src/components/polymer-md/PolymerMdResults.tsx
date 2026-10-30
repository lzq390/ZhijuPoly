import { PolymerMdSelect } from "./PolymerMdSelect";
import { PolymerMdJobTiming, polymerMdTimingIsLive, usePolymerMdClock } from "./PolymerMdJobTiming";
import { PolymerMdRequestSummary } from "./PolymerMdRequestSummary";
import { PolymerMdNumericText } from "./PolymerMdNumericText";
import { PolymerMdStageLogs as StageLogs } from "./PolymerMdStageLogs";
import type { PolymerMdServiceCheck } from "./serviceState";
import { boundedPolymerMdRead, POLYMER_MD_ANALYSIS_TIMEOUT_MS, PolymerMdAnalysisTimeout, polymerMdReadError } from "../../services/polymerMdAnalysisRequest";
import { Activity, Atom, Ban, BarChart3, Download, Files, Gauge, LoaderCircle, Trash2 } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { getSessionEpoch, onSessionRetired } from "../../auth/session";
import type { MdDemoAtomDistanceResponse, MdDemoSeries } from "../../types";
import type { PolymerMdCapabilities, PolymerMdDistance, PolymerMdJob, PolymerMdRequest, PolymerMdStage, PolymerMdTrajectory } from "../../types/polymerMd";
import { calculatePolymerMdDistance, downloadPolymerMdArtifact, fetchPolymerMdTrajectory } from "../../services/polymerMdApi";
import { downloadPolymerMdArtifacts } from "../../services/polymerMdArtifactBundle";
import { clearPolymerMdTrajectoryView, polymerMdTrajectoryIdentity, readPolymerMdResultView, savePolymerMdResultTab, savePolymerMdTrajectoryView, type PolymerMdResultTab } from "../../services/polymerMdViewState";
import { MdSeriesChart } from "../md-simulation/MdSeriesChart";
import { MdTrajectoryExplorer, type MdAtomSelectionSlots } from "../md-simulation/MdTrajectoryExplorer";
import { QUALITY_LABELS, stageLabel, STATUS_LABELS, terminalJob } from "./config";
import { FORCEFIELD_LABELS, NATIVE_CHOICE_FIELDS, requestedCalculationDescription } from "./presentation";
import { polymerMdUserConfigurationSpec } from "./userConfiguration";
import { MODEL_LABELS } from "./PolymerMdModelFields";
import { basisFieldLabel, LockedBasisRules, SavedBasisOverrides } from "./PolymerMdBasisEvidence";
import { polymerMdQualityCodeTitle as qualityCodeTitle, polymerMdQualityMethod, polymerMdQualityReason } from "./qualityPresentation";
import "../../styles/polymer-md-results.css";
const PROPERTY_LABELS: Record<string, [string, string]> = {
  density: ["密度", "g/cm³"],
  density_g_cm3: ["密度", "g/cm³"],
  temp: ["平均温度", "K"],
  temperature: ["平均温度", "K"],
  Cp: ["定压比热容 Cp", "J/(kg·K)"],
  Cv: ["定容比热容 Cv", "J/(kg·K)"],
  Rg: ["回转半径", "Å"],
  r2: ["均方端基距离", "nm²"],
  compressibility: ["等温压缩率", "Pa⁻¹"],
  isentropic_compressibility: ["等熵压缩率", "Pa⁻¹"],
  bulk_modulus: ["等温体积模量", "Pa"],
  isentropic_bulk_modulus: ["等熵体积模量", "Pa"],
  volume_expansion: ["体积热膨胀系数", "K⁻¹"],
  linear_expansion: ["线性热膨胀系数", "K⁻¹"],
  static_dielectric_const: ["固定电荷静态介电常数", ""],
  dielectric_const_dc: ["电子修正静态介电常数", ""],
  refractive_index: ["折射率", ""],
  electronic_corrected_dielectric: ["电子修正静态介电常数", ""],
  total_energy: ["平均总能量", "kJ/mol"],
  "self-diffusion": ["全原子自扩散系数", "m²/s"],
  nematic_order_parameter: ["向列取向序参量", ""],
  thermal_conductivity: ["热导率", "W/(m·K)"],
  thermal_diffusivity: ["热扩散率", "m²/s"],
  tg: ["玻璃化转变温度 Tg", "K"],
  sp_total: ["溶解度参数 SP", "MPa½"],
  sp_ced: ["内聚能密度 CED", "J/cm³"]
};
const numberText = (value: number) => value !== 0 && (Math.abs(value) < .001 || Math.abs(value) >= 1e7) ? value.toExponential(4) : new Intl.NumberFormat("zh-CN", {
  maximumFractionDigits: 5
}).format(value);
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const FAILURE_LABELS: Record<string, string> = {
  engine_process_failed: "引擎执行失败",
  stage_retries_exhausted: "阶段技术重试已用尽"
};
function FailureDiagnostic({ message, code }: { message: unknown; code: unknown }) {
  // Error records may contain private diagnostic metadata. Project only a
  // supplied message or a bounded identifier, never stringify the record.
  const safeErrorCode = typeof code === "string" && /^[a-z][a-z0-9_]{0,95}$/.test(code) ? code : null;
  const text = typeof message === "string" && message.trim() ? message : safeErrorCode ? Object.hasOwn(FAILURE_LABELS, safeErrorCode) ? FAILURE_LABELS[safeErrorCode] : `执行错误（${safeErrorCode}）` : null;
  return text ? <p role="alert">{text}<small className="np-polymer-md-method">请查看对应阶段日志。</small></p> : null;
}
type PropertyRow = {
  key: string; label: string; unit: string; value: number | null; quality?: string;
  reason?: string; method?: string; uncertainty?: number; source?: string; range?: string;
  sourceKey?: string; conditions?: Record<string, unknown>; analysisRange?: Record<string, unknown>;
};
function properties(data: Record<string, unknown>): PropertyRow[] {
  if (Array.isArray(data.properties)) return data.properties.filter(record).filter(p => typeof p.key === "string").map(p => ({
    key: String(p.key),
    label: PROPERTY_LABELS[String(p.key)]?.[0] ?? String(p.key),
    unit: typeof p.unit === "string" ? p.unit : PROPERTY_LABELS[String(p.key)]?.[1] ?? "",
    value: typeof p.value === "number" && Number.isFinite(p.value) ? p.value : null,
    quality: typeof p.quality_status === "string" ? p.quality_status : undefined,
    reason: typeof p.reason === "string" ? p.reason : undefined,
    method: typeof p.method === "string" ? p.method : undefined,
    uncertainty: typeof p.uncertainty === "number" && Number.isFinite(p.uncertainty) ? p.uncertainty : undefined,
    source: typeof p.source_stage === "string" ? stageLabel(p.source_stage) : undefined,
    sourceKey: typeof p.source_stage === "string" ? p.source_stage : undefined,
    conditions: record(p.conditions) ? p.conditions : undefined,
    analysisRange: record(p.analysis_range) ? p.analysis_range : undefined,
    range: record(p.analysis_range) && typeof p.analysis_range.start_ps === "number" && Number.isFinite(p.analysis_range.start_ps) && typeof p.analysis_range.stop_ps === "number" && Number.isFinite(p.analysis_range.stop_ps) && p.analysis_range.stop_ps >= p.analysis_range.start_ps ? `${numberText(p.analysis_range.start_ps)}–${numberText(p.analysis_range.stop_ps)} ps` : undefined
  }));
  const values = {
    ...data
  };
  for (const k of ["properties", "basic", "basic_properties", "equilibrium", "tc", "metrics"]) if (record(data[k])) Object.assign(values, data[k]);
  return Object.entries(PROPERTY_LABELS).filter(([key]) => key in values).map(([key, [label, defaultUnit]]) => {
    const item = values[key],
      itemRecord = record(item) ? item : null;
    const v = itemRecord?.value ?? item;
    return {
      key,
      label,
      unit: typeof itemRecord?.unit === "string" ? itemRecord.unit : defaultUnit,
      value: typeof v === "number" && Number.isFinite(v) ? v : null,
      quality: typeof itemRecord?.quality_status === "string" ? itemRecord.quality_status : undefined,
      reason: typeof itemRecord?.reason === "string" ? itemRecord.reason : undefined,
      method: typeof itemRecord?.method === "string" ? itemRecord.method : undefined,
      sourceKey: typeof itemRecord?.source_stage === "string" ? itemRecord.source_stage : undefined,
      conditions: record(itemRecord?.conditions) ? itemRecord.conditions : undefined,
      analysisRange: record(itemRecord?.analysis_range) ? itemRecord.analysis_range : undefined
    };
  });
}
type EvidenceField = { key: string; label: string; unit?: string; values?: Record<string, string>; kind?: "boolean" | "code" | "hash" | "unit" | "stage" };
type EvidenceRow = [string, string];
// Deliberately project known scientific fields. Never stringify public metadata:
// older snapshots may contain internal paths or process identities in other keys.
const numeric = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value : undefined;
const safeCode = (value: unknown) => typeof value === "string" && value.length <= 250 ? qualityCodeTitle(value) : undefined;
const safeId = (value: unknown) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value) ? value : undefined;
const safeStage = (value: unknown) => typeof value === "string" && /^(?:terminal_qm|monomer_qm|build|eq[123]|additional_\d+|analyze_\d+|tc|tc_analyze|sp|sp_analyze|tg|tg_analyze)$/.test(value) ? value : undefined;
const qualityText = (value: unknown) => typeof value === "string" && Object.hasOwn(QUALITY_LABELS, value) ? QUALITY_LABELS[value as keyof typeof QUALITY_LABELS] : "未提供质量判定";
const TACTICITY_LABELS = { atactic: "无规", isotactic: "全同", syndiotactic: "间同", none: "未识别出立构类型" };
function evidenceRows(value: unknown, fields: EvidenceField[]): EvidenceRow[] {
  if (!record(value)) return [];
  return fields.flatMap(field => {
    const raw = value[field.key];
    let text: string | undefined;
    if (field.values) text = typeof raw === "string" && Object.hasOwn(field.values, raw) ? field.values[raw] : undefined;
    else if (field.kind === "boolean") text = typeof raw === "boolean" ? raw ? "是" : "否" : undefined;
    else if (field.kind === "hash") text = typeof raw === "string" && /^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(raw) ? raw : undefined;
    else if (field.kind === "unit") text = typeof raw === "string" && ["1", "g/cm3", "g/cm³", "K", "atm", "J/(kg K)", "J/(kg·K)", "W/(m K)", "W/(m·K)", "m2/s", "m²/s", "1/Pa", "1/K", "Pa", "A", "Å", "nm2", "kJ/mol", "MPa^0.5", "MPa½", "J/cm3", "J/cm³"].includes(raw) ? raw : undefined;
    else if (field.kind === "code") text = safeCode(raw);
    else if (field.kind === "stage") text = safeStage(raw) ? stageLabel(String(raw)) : undefined;
    else if (numeric(raw) !== undefined) text = `${numberText(raw as number)}${field.unit ? ` ${field.unit}` : ""}`;
    return text === undefined ? [] : [[field.label, text] as EvidenceRow];
  });
}
function EvidenceList({ rows, empty = "此记录未提供相关证据。" }: { rows: EvidenceRow[]; empty?: string }) {
  return rows.length ? <dl className="np-polymer-md-evidence-list">{rows.map(([label, value], index) => <div key={`${label}:${index}`}><dt>{label}</dt><dd className={typeof value === "number" || typeof value === "string" && value.length > 0 && !/[\u3400-\u9fff]/.test(value) ? "np-pmd-code" : undefined}>{value}</dd></div>)}</dl> : <p className="np-polymer-md-muted">{empty}</p>;
}
function WindowEvidence({ value }: { value: unknown }) {
  const window = record(value) ? value : {};
  const start = numeric(window.start_ps), stop = numeric(window.stop_ps);
  const rows: EvidenceRow[] = [["实际分析时间窗", start !== undefined && stop !== undefined && stop >= start ? `${numberText(start)}–${numberText(stop)} ps` : "未提供有效分析时间窗"]];
  rows.push(...evidenceRows(window, [
    { key: "requested_start_ps", label: "要求的分析起点", unit: "ps" },
    { key: "observed_start_ps", label: "原始观测起点", unit: "ps" }, { key: "observed_stop_ps", label: "原始观测终点", unit: "ps" },
    { key: "sample_count", label: "实际分析样本数" }
  ]));
  return <EvidenceList rows={rows} />;
}
const CONDITION_FIELDS: EvidenceField[] = [
  { key: "temperature_k", label: "分析温度", unit: "K" }, { key: "pressure_atm", label: "分析压力", unit: "atm" },
  { key: "ensemble", label: "系综", values: { NPT: "NPT", NVT: "NVT", NVE: "NVE" } },
  { key: "axis", label: "传热方向", values: { x: "x", y: "y", z: "z" } }, { key: "decomp", label: "热流分解", kind: "boolean" },
  { key: "time_step_fs", label: "时间步长", unit: "fs" }, { key: "burn_in_ps", label: "丢弃的瞬态时长", unit: "ps" },
  { key: "frames", label: "分析轨迹帧数" }, { key: "unwrapped", label: "展开周期坐标", kind: "boolean" },
  { key: "fit_r2", label: "拟合 R²" }, { key: "lorentz_lorenz_phi", label: "Lorentz–Lorenz 极化分数" },
  { key: "temperature_min_k", label: "扫描最低温度", unit: "K" }, { key: "temperature_max_k", label: "扫描最高温度", unit: "K" },
  { key: "temperature_interval_k", label: "扫描温度间隔", unit: "K" }, { key: "plateau_duration_ps", label: "每温度平台时长", unit: "ps" },
  { key: "temperature_interval_duration_ps", label: "每温度间隔总时长", unit: "ps" },
  { key: "nominal_cooling_rate_k_per_ns", label: "名义降温速率", unit: "K/ns" },
  { key: "sampling_ns", label: "追加采样时长", unit: "ns" }, { key: "heating_equilibration_ns", label: "高温平衡时长", unit: "ns" },
  { key: "sample_interval_ps", label: "观测输出间隔", unit: "ps" }, { key: "block_count", label: "误差估计区块数" },
  { key: "density_curve_point_count", label: "温度—密度数据点数" },
  { key: "source_equilibrium_stage", label: "采用的基础平衡末态", kind: "stage" },
  { key: "n_chain", label: "参与分析的链数" }, { key: "decomposition", label: "能量分解", kind: "boolean" },
  { key: "TALLY_required", label: "需要 LAMMPS TALLY", kind: "boolean" }, { key: "pair_tail_correction", label: "成对相互作用尾修正", kind: "boolean" },
  { key: "cohesive_definition", label: "内聚能密度定义", values: {
    minus_inter_molecular_nonbonded_energy_over_volume: "内聚能密度 = −分子间非键相互作用能 / 体积"
  } },
  { key: "energy_observation", label: "分子间能量来源", values: {
    pair_inter_plus_long_total_minus_sum_chain_long_self: "分子间成对非键能 + 总长程能 − 各链长程自能之和"
  } },
  { key: "averaging", label: "溶解度参数平均方式", values: {
    "mean_of_per_frame_sqrt_CED;matches_native_Solubility_Parameter": "每帧内聚能密度开平方后取平均（原生 SP 定义）"
  } },
  { key: "density_conversion_kg_m3_per_g_cm3", label: "密度单位换算因子" },
  { key: "observable", label: "观测量范围", values: { all_atom_not_chain_COM: "全原子 MSD（非链质心）", single_axis_finite_size_single_run: "单方向、有限尺寸、单次运行" } },
  { key: "time_basis", label: "分析时间基准", values: { production_Step_times_dt: "生产步数 × 时间步长" } },
  { key: "uncertainty_scope", label: "误差范围", values: { within_run_time_blocks_no_size_extrapolation_or_independent_seeds: "单次运行的时间区块；不含尺寸外推或独立种子误差" } },
  { key: "uncertainty_model", label: "误差估计与传播", values: {
    not_propagated_from_QM_and_MD_dependencies: "未传播 QM 与 MD 依赖误差",
    "not_propagated_from_kappa_density_Cp;cross_stage_covariances_not_estimated": "未传播 κ、密度和 Cp 的全部误差；未估计跨阶段协方差",
    "five_contiguous_time_blocks;not_independent_frames": "单次轨迹分为 5 个连续时间块估计标准误差；时间相关帧不作为独立样本，也不包含独立种子重复的误差。",
    "five_contiguous_time_blocks_per_temperature;not_independent_seeds": "每个温度平台内分为 5 个连续时间块估计标准误差；不代表独立种子重复，也不包含降温速率敏感性。"
  } }
];
function PropertyConditions({ value }: { value: unknown }) {
  const data = record(value) ? value : {};
  const dependencies = record(data.dependencies) ? data.dependencies : {};
  const depKeys = ["density", "Cp", "thermal_conductivity", "static_dielectric_const", "refractive_index"];
  const deps = depKeys.flatMap(key => record(dependencies[key]) ? [[key, dependencies[key]] as const] : record(data[key]) ? [[key, data[key]] as const] : []);
  return <><EvidenceList rows={evidenceRows(data, CONDITION_FIELDS)} empty="未提供可展示的性质计算条件。" /><SavedBasisOverrides value={data.basis_gen} />{record(data.qm) ? <section aria-label="QM 派生输入"><h5>QM 派生输入</h5><EvidenceList rows={evidenceRows(data.qm, [
    { key: "polarizability_angstrom3", label: "静态极化率", unit: "Å³" }, { key: "mol_weight_g_mol", label: "分子量", unit: "g/mol" },
    { key: "polarizability_method", label: "极化率方法", kind: "code" }, { key: "mol_weight_method", label: "分子量方法", kind: "code" }
  ])} /><SavedBasisOverrides value={data.qm.basis_gen} /></section> : null}{deps.map(([key, dependency]) => <section key={key} aria-label={`${PROPERTY_LABELS[key]?.[0] ?? key}依赖来源`}><h5>{PROPERTY_LABELS[key]?.[0] ?? key}依赖来源</h5><EvidenceList rows={[
    ["依赖质量", qualityText(dependency.quality_status)], ["依赖来源阶段", safeStage(dependency.source_stage) ? stageLabel(String(dependency.source_stage)) : "未提供来源阶段"],
    ...evidenceRows(dependency, [{ key: "value", label: "依赖数值" }, { key: "uncertainty", label: "依赖标准误差" }, { key: "unit", label: "依赖单位", kind: "unit" }, { key: "method", label: "依赖方法", kind: "code" }])
  ]} /><WindowEvidence value={dependency.analysis_range} /><EvidenceList rows={evidenceRows(dependency.conditions, CONDITION_FIELDS)} empty="未提供依赖分析条件。" /></section>)}</>;
}
const EFFECTIVE_FIELDS: EvidenceField[] = [
  { key: "units", label: "LAMMPS 单位体系", values: { real: "real" } }, { key: "axis", label: "实际传热方向", values: { x: "x", y: "y", z: "z" } },
  { key: "rep", label: "传热方向复制倍数" }, { key: "rep_other", label: "横向复制倍数" }, { key: "time_step_fs", label: "实际时间步长", unit: "fs" },
  { key: "steps", label: "生产步数" }, { key: "target_temperature_k", label: "实际目标温度", unit: "K" }, { key: "slab_count", label: "温度层数" },
  { key: "exchange_steps", label: "热交换间隔", unit: "步" }, { key: "exchange_interval_ps", label: "热交换时间间隔", unit: "ps" },
  { key: "profile_nevery_steps", label: "温度层采样间隔", unit: "步" }, { key: "profile_nrepeat", label: "温度层平均重复数" }, { key: "profile_steps", label: "温度层输出间隔", unit: "步" },
  { key: "thermo_steps", label: "热力学输出间隔", unit: "步" }, { key: "thermo_interval_ps", label: "热力学时间间隔", unit: "ps" },
  { key: "trajectory_steps", label: "轨迹输出间隔", unit: "步" }, { key: "xtc_steps", label: "XTC 输出间隔", unit: "步" }, { key: "xtc_interval_ps", label: "XTC 时间间隔", unit: "ps" },
  { key: "preheat_steps", label: "预热步数" }, { key: "preheat_ps", label: "预热时长", unit: "ps" }, { key: "velocity_gaussian_seed", label: "实际速度随机种子" },
  { key: "decomposition", label: "热流分解", kind: "boolean" }, { key: "bulk_ascii_dump", label: "ASCII 轨迹输出", kind: "boolean" },
  { key: "compatibility_patch", label: "输入兼容补丁", values: { fixed_output_boolean_guards: "固定 0/1 输出条件" } },
  { key: "sampling_policy", label: "采样协议", values: { "locked_RNEMD_cadence;request_sample_interval_ps_applies_to_EQ_only": "RNEMD 固定采样；请求采样间隔仅用于平衡阶段" } },
  { key: "input_sha256", label: "执行输入 SHA256", kind: "hash" }, { key: "upstream_input_sha256", label: "上游原始输入 SHA256", kind: "hash" }
];
function effectiveEvidence(value: unknown, stage: string): EvidenceRow[] {
  const fields = ["sp", "tg"].includes(stage)
    ? [...EFFECTIVE_FIELDS.filter(field => !CONDITION_FIELDS.some(existing => existing.key === field.key)), ...CONDITION_FIELDS]
    : EFFECTIVE_FIELDS;
  return evidenceRows(value, fields);
}
function TgPlateauWindows({ value }: { value: unknown }) {
  if (!Array.isArray(value) || !value.length) return null;
  const bounded = value.slice(0, 100);
  const windows = bounded.filter(record).filter(item => numeric(item.temperature_k) !== undefined && Number(item.temperature_k) > 0
    && numeric(item.start_ps) !== undefined && numeric(item.stop_ps) !== undefined && Number(item.stop_ps) >= Number(item.start_ps)
    && typeof item.sample_count === "number" && Number.isSafeInteger(item.sample_count) && item.sample_count > 0);
  return <section aria-label="Tg 温度平台分析窗口"><details><summary>逐温度平台分析窗口（{windows.length} 项）</summary>
    {windows.length ? <div className="np-polymer-md-table-wrap"><table><thead><tr><th>温度 (K)</th><th>起点 (ps)</th><th>终点 (ps)</th><th>有效样本数</th></tr></thead><tbody>{windows.map((item, index) => <tr key={index}><th scope="row">{numberText(Number(item.temperature_k))}</th><td>{numberText(Number(item.start_ps))}</td><td>{numberText(Number(item.stop_ps))}</td><td>{numberText(Number(item.sample_count))}</td></tr>)}</tbody></table></div> : <p className="np-polymer-md-muted">未提供有效温度平台分析记录。</p>}
    {windows.length !== bounded.length ? <p className="np-polymer-md-muted">部分平台记录缺少有效温度、时间窗或样本数，未展示无效条目。</p> : null}
    {value.length > bounded.length ? <p className="np-polymer-md-muted">仅展示前 100 条平台记录；完整记录见分析产物。</p> : null}
  </details></section>;
}
function GradientEvidence({ value, label }: { value: unknown; label: string }) {
  const fit = record(value) ? value : {};
  return <section aria-label={label}><h5>{label}</h5><EvidenceList rows={evidenceRows(fit, [
    { key: "slope_k_m", label: "温度梯度斜率", unit: "K/m" }, { key: "intercept_k", label: "拟合截距", unit: "K" }, { key: "r2", label: "拟合 R²" },
    { key: "slope_standard_error_k_m", label: "斜率标准误差", unit: "K/m" }, { key: "intercept_standard_error_k", label: "截距标准误差", unit: "K" }
  ])} empty="未提供该侧温度梯度拟合。" /><EvidenceList rows={evidenceRows(fit.fit_window, [
    { key: "start_m", label: "空间拟合起点", unit: "m" }, { key: "stop_m", label: "空间拟合终点", unit: "m" }, { key: "sample_count", label: "拟合温度层数" }
  ])} empty="未提供空间拟合区间。" /><WindowEvidence value={fit.profile_time_window} /></section>;
}
function QualityEvidence({ value, thermal }: { value: unknown; thermal: boolean }) {
  const data = record(value) ? value : {};
  const checks = record(data.checks) ? data.checks : {};
  const fits = record(data.temperature_gradient_fits) ? data.temperature_gradient_fits : {};
  const exchange = record(data.exchange_energy_fit) ? data.exchange_energy_fit : {};
  const reasons = Array.isArray(data.reasons) ? data.reasons.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
  const estimates = Array.isArray(data.conductivity_block_estimates) && data.conductivity_block_estimates.length <= 5 && data.conductivity_block_estimates.every(v => numeric(v) !== undefined) ? data.conductivity_block_estimates as number[] : [];
  return <><EvidenceList rows={evidenceRows(data, [
    { key: "left_r2", label: "左侧温度梯度 R²" }, { key: "right_r2", label: "右侧温度梯度 R²" }, { key: "qualified_block_rate", label: "温度块合格比例" },
    { key: "qualified_profile_blocks", label: "合格温度块数" }, { key: "profile_blocks", label: "全部温度块数" }, { key: "production_profile_blocks", label: "生产温度块数" },
    { key: "production_thermo_samples", label: "生产热力学样本数" }, { key: "exchange_r2", label: "交换能量拟合 R²" },
    { key: "gradient_k_m", label: "两侧平均梯度绝对值", unit: "K/m" }, { key: "heat_flux_w_m2", label: "热通量绝对值", unit: "W/m²" },
    { key: "fit_r2", label: "拟合 R²" }, { key: "fit_rmse_g_cm3", label: "密度拟合均方根误差", unit: "g/cm³" },
    { key: "low_temperature_slope", label: "低温侧密度斜率", unit: "g/(cm³·K)" }, { key: "high_temperature_slope", label: "高温侧密度斜率", unit: "g/(cm³·K)" },
    { key: "intersection_k", label: "两侧拟合交点", unit: "K" }, { key: "temperature_min_k", label: "最低拟合温度", unit: "K" },
    { key: "temperature_max_k", label: "最高拟合温度", unit: "K" }, { key: "density_curve_point_count", label: "温度—密度点数" },
    { key: "block_count", label: "误差估计区块数" }, { key: "sample_count", label: "有效样本数" }, { key: "relative_standard_error", label: "相对标准误差" }
  ])} empty={thermal ? "未提供 RNEMD 拟合数值；不能据任务状态推断热导质量。" : "此阶段未提供汇总拟合数值。"} />
  {Object.keys(checks).length ? <section aria-label="基础平衡判据"><h5>基础平衡判据</h5><EvidenceList rows={evidenceRows(checks, [{ key: "sample_count", label: "生产样本数" }, { key: "burn_in_ps", label: "丢弃的瞬态时长", unit: "ps" }, { key: "finite", label: "观测值均为有限数", kind: "boolean" }])} /><WindowEvidence value={checks.analysis_range} />{["TotEng", "E_bond", "E_angle", "E_dihed", "E_vdwl", "Density"].filter(key => record(checks[key])).map(key => <section key={key}><h5>{({ TotEng: "总能量", E_bond: "键能", E_angle: "键角能", E_dihed: "二面角能", E_vdwl: "范德华能", Density: "密度" } as Record<string, string>)[key]}稳定性</h5><EvidenceList rows={evidenceRows(checks[key], [{ key: "block_mean_sd", label: "区块均值标准差" }, { key: "limit", label: "服务端判据上限" }, { key: "passed", label: "判据通过", kind: "boolean" }])} /></section>)}</section> : null}
  {record(data.trajectory) ? <section aria-label="构象与取向判据"><h5>构象与取向判据</h5><EvidenceList rows={evidenceRows(data.trajectory, [{ key: "production_frame_count", label: "生产轨迹帧数" }, { key: "structure_equilibrated", label: "构象平衡判据通过", kind: "boolean" }, { key: "nematic_order_below_0_1", label: "向列取向小于 0.1", kind: "boolean" }])} /><WindowEvidence value={data.trajectory.analysis_range} /></section> : null}
  {thermal ? <><WindowEvidence value={data.analysis_range} /><EvidenceList rows={evidenceRows(data.clock, [{ key: "native_time_offset_ps", label: "原生生产时间偏移", unit: "ps" }, { key: "observed_step_start", label: "生产观测首步" }, { key: "observed_step_stop", label: "生产观测末步" }])} empty="未提供生产时间轴检查。" /><EvidenceList rows={evidenceRows(data.profile_averaging, [{ key: "interval_steps", label: "温度块平均间隔", unit: "步" }, { key: "interval_ps", label: "温度块平均时间", unit: "ps" }, { key: "fixed_interval_verified", label: "固定平均间隔已验证", kind: "boolean" }])} empty="未提供温度块平均协议。" /><EvidenceList rows={evidenceRows(data.production_profile_support, [{ key: "start_ps", label: "有效温度块支撑起点", unit: "ps" }, { key: "stop_ps", label: "有效温度块支撑终点", unit: "ps" }, { key: "block_count", label: "有效温度块数" }])} empty="未提供温度块时间支撑区间。" /><GradientEvidence label="左侧温度梯度拟合" value={fits.left} /><GradientEvidence label="右侧温度梯度拟合" value={fits.right} /><section aria-label="交换能量拟合"><h5>交换能量拟合</h5><EvidenceList rows={evidenceRows(exchange, [{ key: "slope_w_m2", label: "交换能量斜率", unit: "W/m²" }, { key: "intercept_j_m2", label: "交换能量截距", unit: "J/m²" }, { key: "r2", label: "拟合 R²" }, { key: "slope_standard_error_w_m2", label: "斜率标准误差", unit: "W/m²" }])} empty="未提供交换能量拟合。" /><WindowEvidence value={exchange.fit_window} /></section>{estimates.length ? <EvidenceList rows={[["热导区块估计", `${estimates.map(numberText).join("、")} W/(m·K)`]]} /> : <p className="np-polymer-md-muted">未提供有效的热导区块估计。</p>}</> : null}
  <TgPlateauWindows value={data.plateau_analysis_windows} />
  {reasons.length ? <p title={qualityCodeTitle(reasons.join(";"))}>质量原因：{polymerMdQualityReason(reasons.join(";"))}</p> : <p className="np-polymer-md-muted">未提供质量原因代码；不据此判定通过。</p>}</>;
}
function requestedModelDescription(request: PolymerMdRequest): string {
  const model = request.model_configuration;
  if (!model) return `${request.smiles} · ${request.n_atom} 目标原子/链`;
  const length = (value: { mode: string; value: number }) => value.mode === "atoms" ? `${value.value} 目标原子/链` : value.mode === "molecular_weight" ? `${value.value} g/mol` : `${value.value} ${model.mode === "alternating" ? "交替周期" : "DP"}`;
  const units = model.components.map((component, index) => `${component.id}: ${component.smiles}${model.mode === "blend" ? `（${component.n_chain}条，${length(component.chain_length!)}）` : model.mode === "random" ? `（重复单元数比例 ${numberText((model.ratios?.[index] ?? 0) * 100)}%）` : ""}`).join(" / ");
  const sizing = model.chain_length ? length(model.chain_length) : model.blocks ? model.blocks.map(block => `${block.component_id}×${block.dp}`).join("–") : "";
  return `${MODEL_LABELS[model.mode]} · ${units}${sizing ? ` · ${sizing}` : ""}`;
}
function CompositionEvidence({ job, model }: { job: PolymerMdJob; model: unknown }) {
  const configuration = job.scientific_request.model_configuration;
  if (!configuration || !record(model)) return null;
  const composition = Array.isArray(model.actual_composition) ? model.actual_composition.filter(record) : [];
  const species = Array.isArray(model.chain_species) ? model.chain_species.filter(record).slice(0, 20) : [];
  const identifiers = new Set(configuration.components.map(component => component.id));
  return <section aria-label="实际组成与分子量统计"><h4>实际组成与分子量统计</h4>
    <p>模型类型：{MODEL_LABELS[configuration.mode]}。下列统计描述本次构建模型。</p>
    <p className="np-polymer-md-muted">实际立构为 RadonPy 对构建链的原生分类。适用性取决于主链手性中心；当前记录未提供适用中心计数，不能仅据分类判定立构控制是否适用，也不表示结晶度。</p>
    <EvidenceList rows={evidenceRows(model.polymer_statistics, [{ key: "Mn", label: "模型数均分子量 Mn", unit: "g/mol" }, { key: "Mw", label: "模型重均分子量 Mw", unit: "g/mol" }, { key: "dispersity", label: "模型分散度 Mw/Mn" }, { key: "n_mol", label: "模型分子数" }])} />
    {configuration.components.map(component => <section key={component.id}><h5>重复单元 {component.id}：<code className="np-pmd-code">{component.smiles}</code></h5><EvidenceList rows={evidenceRows(composition.find(item => item.id === component.id), [{ key: "repeat_unit_count", label: "实际重复单元总数" }, { key: "repeat_unit_mole_fraction", label: "实际重复单元数分数" }])} /></section>)}
    {species.map((item, index) => {
      const sequence = Array.isArray(item.sequence) && item.sequence.every(value => typeof value === "string" && identifiers.has(value)) ? item.sequence : [];
      return <details key={index}><summary>链种 {index + 1} 的实际构建</summary><EvidenceList rows={evidenceRows(item, [{ key: "n_chain", label: "链数" }, { key: "actual_dp", label: "实际聚合度 DP" }, { key: "atoms_per_chain", label: "每链实际原子数" }, { key: "molecular_weight_g_mol", label: "每链实际分子量", unit: "g/mol" }, { key: "actual_tacticity", label: "实际立构（原生分类）", values: TACTICITY_LABELS }])} />{sequence.length ? <p>重复单元序列：{sequence.slice(0, 40).join("–")}{sequence.length > 40 ? `…（共 ${sequence.length} 个，完整序列见建模产物）` : ""}</p> : null}</details>;
    })}
  </section>;
}
function NativeConfigurationEvidence({ value, requested = false, applicability, stage, status }: {
  value: unknown; requested?: boolean; applicability?: unknown; stage?: string; status?: string;
}) {
  if (!record(value)) return <p className="np-polymer-md-muted">{requested ? "历史请求未保存独立量化配置；实际方法以对应阶段记录为准。" : "此阶段未提供实际量化配置，不据请求推断已执行。"}</p>;
  const targets = Array.isArray(value.properties) ? value.properties : [];
  const singlePoint = targets.some(key => ["qm_total_energy", "qm_homo", "qm_lumo", "qm_dipole"].includes(String(key)));
  const rows: EvidenceRow[] = NATIVE_CHOICE_FIELDS.filter(field => singlePoint || !["sp_method", "sp_basis"].includes(field.id)).flatMap(field => {
    const option = field.options.find(item => item.value === value[field.id]);
    return option ? [[basisFieldLabel(field), option.label] as EvidenceRow] : [];
  });
  const propertyField = polymerMdUserConfigurationSpec.fields.find(field => field.id === "properties");
  const labels = propertyField?.options.filter(option => targets.includes(option.value)).map(option => option.label) ?? [];
  const chargeUnused = ["gasteiger", "zero"].includes(String(value.charge));
  return <><EvidenceList rows={rows} empty="未提供可识别的量化方法与基组。" />
    {chargeUnused ? <p className="np-polymer-md-muted">所选电荷模型不使用量化电荷方法与基组。</p> : null}
    {!targets.some(key => key === "refractive_index" || key === "qm_polarizability") ? <p className="np-polymer-md-muted">未选择需要极化率的目标；保留的方法配置不代表已经执行极化率计算。</p> : null}
    {!requested ? <p className="np-polymer-md-muted">以上为该阶段保存的配置；收敛与计算结果以阶段质量证据为准。</p> : null}
    {!requested ? <LockedBasisRules configuration={value} applicability={applicability} stage={stage} status={status} /> : null}
    {labels.length ? <p>保存的目标字段：{labels.join("、")}。</p> : null}
  </>;
}
function RunEvidence({ job, rows }: { job: PolymerMdJob; rows: PropertyRow[] }) {
  const request = job.scientific_request;
  const buildStage = [...job.stage_runs].reverse().find(stage => stage.stage === "build" && stage.status === "completed");
  const model = buildStage && record(buildStage.results.model) ? buildStage.results.model : buildStage?.results;
  const summary = record(job.results.quality_summary) ? job.results.quality_summary : {};
  const qualityCategory = (name: string) => ({ tc_analyze: "thermal", sp_analyze: "sp", tg_analyze: "glass_transition" }[name] ?? "basic");
  const analyses = job.stage_runs.filter(stage => /^analyze_\d+$/.test(stage.stage) || ["tc_analyze", "sp_analyze", "tg_analyze"].includes(stage.stage));
  const unmatchedSummaries = ["basic", "thermal", "sp", "glass_transition"].flatMap(category => {
    const quality = summary[category];
    return record(quality) && !analyses.some(stage => stage.stage_run_id === quality.stage_run_id) ? [{ category, quality }] : [];
  });
  const propertySources = [...(rows.length ? [{ title: "当前汇总性质", rows }] : []), ...job.stage_runs.flatMap(stage => {
    const stageRows = properties(stage.results);
    return stageRows.length ? [{ title: `${stageLabel(stage.stage)} · ${safeId(stage.stage_run_id) ?? "阶段标识未提供"}`, rows: stageRows }] : [];
  })];
  return <details className="np-polymer-md-evidence"><summary>运行条件与质量证据</summary><p className="np-polymer-md-muted">请求值与实际执行证据分别记录。未返回的字段不补为默认值；阶段完成不等于性质质量通过。</p><section aria-label="持久化科学请求"><h3>持久化科学请求</h3><EvidenceList rows={[
    ["计算范围", requestedCalculationDescription(request)],
    ["链组成与长度", requestedModelDescription(request)],
    ...(!request.model_configuration || request.model_configuration.chain_length?.mode === "atoms" ? evidenceRows(request, [{ key: "n_atom", label: "每链目标原子数" }]) : []), ...evidenceRows(request, [
      { key: "temp", label: "目标温度", unit: "K" }, { key: "pressure", label: "目标压力", unit: "atm" }, { key: "n_chain", label: "请求链数" },
      { key: "forcefield", label: "请求力场", values: FORCEFIELD_LABELS }, { key: "terminal_head", label: "头端基", values: { CH3: "CH3", H: "H", OH: "OH" } }, { key: "terminal_tail", label: "尾端基", values: { CH3: "CH3", H: "H", OH: "OH" } },
      { key: "tacticity", label: "请求立构", values: { atactic: "无规", isotactic: "全同", syndiotactic: "间同" } },
      { key: "eq_ns", label: "EQ3 请求时长", unit: "ns" }, { key: "additional_ns", label: "每次追加请求时长", unit: "ns" }, { key: "max_additional", label: "最多追加次数" },
      { key: "sample_interval_ps", label: "平衡阶段请求采样间隔", unit: "ps" }, { key: "seed", label: "任务实际随机种子" }
    ]), ...(request.calculation === "basic_tc" ? evidenceRows(request, [{ key: "tc_axis", label: "RNEMD 请求方向", values: { x: "x", y: "y", z: "z" } }, { key: "tc_ns", label: "RNEMD 请求生产时长", unit: "ns" }, { key: "tc_rep", label: "RNEMD 请求复制倍数" }]) : [])
  ]} /><h4>请求的量化方法与目标</h4><NativeConfigurationEvidence value={request.native_configuration} requested /></section><section aria-label="实际构建模型"><h3>实际构建模型</h3><EvidenceList rows={evidenceRows(model, [
    { key: "actual_dp", label: "实际聚合度 DP" }, { key: "atoms_per_chain", label: "实际每链原子数" }, { key: "actual_atom_count", label: "实际基础体系原子数" }, { key: "n_chain", label: "实际链数" },
    { key: "initial_density_g_cm3", label: "初始装箱密度", unit: "g/cm³" }, { key: "actual_tacticity", label: "实际立构（原生分类）", values: TACTICITY_LABELS },
    { key: "forcefield", label: "实际力场", values: FORCEFIELD_LABELS }
  ])} empty="未提供已完成建模阶段的实际规模；请求原子数不等于实际原子数。" /><CompositionEvidence job={job} model={model} /></section><section aria-label="阶段协议与运行时来源"><h3>阶段协议与运行时来源</h3>{job.stage_runs.length ? job.stage_runs.map(stage => <details key={stage.stage_run_id}><summary>{stageLabel(stage.stage)} · 第 {stage.attempt} 次 · {qualityText(stage.quality_status)}</summary><EvidenceList rows={[
    ["来源阶段标识", safeId(stage.stage_run_id) ?? "未提供有效阶段标识"],
    ["运行时指纹", evidenceRows(stage.results, [{ key: "runtime_fingerprint", label: "运行时指纹", kind: "hash" }])[0]?.[1] ?? "此阶段未提供运行时指纹"],
    ...evidenceRows(stage.results, [{ key: "source_sha", label: "RadonPy 源码 SHA", kind: "hash" }, { key: "patch_version", label: "科学适配器版本", kind: "code" }])
  ]} />{["terminal_qm", "monomer_qm"].includes(stage.stage) ? <NativeConfigurationEvidence value={stage.results.effective_native_configuration} applicability={stage.results.parameter_applicability} stage={stage.stage} status={stage.status} /> : null}<EvidenceList rows={effectiveEvidence(stage.results.effective_config, stage.stage)} empty="此阶段未提供实际执行协议；不能用当前服务默认值代替。" /></details>) : <p>尚无阶段执行记录。</p>}</section><section aria-label="性质来源与计算条件"><h3>性质来源与计算条件</h3>{propertySources.length ? propertySources.map((group, index) => <section key={index}><h4>{group.title}</h4>{group.rows.map((property, propertyIndex) => <details key={`${property.key}:${propertyIndex}`}><summary>{property.label} · {qualityText(property.quality)}</summary><EvidenceList rows={[
    ["计算方法", safeCode(property.method) ?? "未提供可展示的计算方法"], ["来源阶段", safeStage(property.sourceKey) ? stageLabel(property.sourceKey!) : "未提供来源阶段"],
    ["报告的误差估计", property.uncertainty !== undefined ? `${numberText(property.uncertainty)} ${evidenceRows({ unit: property.unit }, [{ key: "unit", label: "单位", kind: "unit" }])[0]?.[1] ?? ""}`.trim() : "未提供误差估计"]
  ]} /><WindowEvidence value={property.analysisRange} /><PropertyConditions value={property.conditions} /></details>)}</section>) : <p>尚无性质来源记录。</p>}</section><section aria-label="阶段质量判据"><h3>阶段质量判据</h3>{analyses.map(stage => {
    const fallback = summary[qualityCategory(stage.stage)];
    const knownQuality = (value: unknown) => record(value) && ["checks", "reasons", "analysis_range", "left_r2", "production_profile_blocks", "temperature_gradient_fits", "plateau_analysis_windows", "fit_r2"].some(key => Object.hasOwn(value, key));
    const quality = knownQuality(stage.quality) ? stage.quality : knownQuality(stage.results.quality) ? stage.results.quality : record(fallback) && fallback.stage_run_id === stage.stage_run_id ? fallback : {};
    return <details key={stage.stage_run_id}><summary>{stageLabel(stage.stage)} · {safeId(stage.stage_run_id) ?? "阶段标识未提供"} · {qualityText(stage.quality_status)}</summary><QualityEvidence value={quality} thermal={stage.stage === "tc_analyze"} /></details>;
  })}{unmatchedSummaries.map(({ category, quality }) => <details key={`summary:${category}`}><summary>{({ thermal: "RNEMD", sp: "SP／内聚能", glass_transition: "Tg" } as Record<string, string>)[category] ?? "基础物性"}汇总质量 · {qualityText(quality.quality_status)}</summary><p className="np-polymer-md-muted">仅保留汇总质量，缺少对应分析阶段记录；不据当前服务补写来源。</p><EvidenceList rows={[["汇总来源阶段标识", safeId(quality.stage_run_id) ?? "未提供来源阶段标识"]]} /><QualityEvidence value={quality} thermal={category === "thermal"} /></details>)}{!analyses.length && !unmatchedSummaries.length ? <p>尚无分析阶段质量证据；历史或未完成任务可能未提供。</p> : null}{request.calculation === "basic_tc" && !analyses.some(stage => stage.stage === "tc_analyze") && !unmatchedSummaries.some(item => item.category === "thermal") ? <p>尚无 RNEMD 分析阶段质量证据；未执行、失败或中断时不会推测热导拟合结果。</p> : null}</section></details>;
}
type DisplaySeries = MdDemoSeries & { spatial?: boolean; temperature?: boolean };
function seriesFrom(data: Record<string, unknown>): DisplaySeries[] {
  const source = Array.isArray(data.series) ? data.series : record(data.series) ? Object.values(data.series) : [];
  return source.filter(record).flatMap(s => {
    if (typeof s.key !== "string" || typeof s.label !== "string" || typeof s.unit !== "string" || !Array.isArray(s.points) || !s.points.length) return [];
    const spatial = s.points.every(p => record(p) && typeof p.position_angstrom === "number" && Number.isFinite(p.position_angstrom));
    const temperature = !spatial && s.points.every(p => record(p) && typeof p.temperature_k === "number" && Number.isFinite(p.temperature_k));
    const xKey = spatial ? "position_angstrom" : temperature ? "temperature_k" : "time_ps";
    if (!s.points.every(p => record(p) && typeof p[xKey] === "number" && Number.isFinite(p[xKey]) && typeof p.value === "number" && Number.isFinite(p.value))) return [];
    return [{ key: s.key, label: s.key === "temperature_profile" ? "稳态温度分布" : s.label, unit: s.unit, spatial, temperature,
      points: s.points.map(p => ({ time_ps: Number(p[xKey]), value: Number(p.value) })) }];
  });
}
function StageProgress({
  stage
}: {
  stage: PolymerMdStage;
}) {
  const progress = record(stage.results.progress) ? stage.results.progress : {};
  const current = progress.current_step ?? stage.results.current_step ?? stage.results.step ?? stage.quality.current_step;
  const total = progress.total_steps ?? stage.results.total_steps ?? stage.quality.total_steps;
  return typeof current === "number" && Number.isFinite(current) ? <span className="np-pmd-stage-progress"><PolymerMdNumericText>{`已完成 ${current.toLocaleString()} 步${typeof total === "number" && Number.isFinite(total) ? ` / ${total.toLocaleString()} 步` : ""}`}</PolymerMdNumericText></span> : null;
}
function DistanceSampling({ value }: { value: unknown }) {
  const sampling = record(value) ? value : {};
  const keys = ["source_frame_count", "start_frame", "stop_frame", "stride", "sampled_frames", "first_frame", "last_frame", "start_ps", "stop_ps"];
  if (!keys.every(key => numeric(sampling[key]) !== undefined) || Number(sampling.stride) < 1 || Number(sampling.sampled_frames) < 1) return <p className="np-polymer-md-muted">未提供有效的距离采样范围。</p>;
  return <p aria-label="原子距离采样范围" className="np-polymer-md-muted">源轨迹 {numberText(Number(sampling.source_frame_count))} 帧；请求分析第 {numberText(Number(sampling.start_frame) + 1)}–{numberText(Number(sampling.stop_frame))} 帧，步长 {numberText(Number(sampling.stride))}；实际 {numberText(Number(sampling.sampled_frames))} 个采样点（第 {numberText(Number(sampling.first_frame) + 1)}–{numberText(Number(sampling.last_frame) + 1)} 帧），时间 {numberText(Number(sampling.start_ps))}–{numberText(Number(sampling.stop_ps))} ps。</p>;
}
function Trajectory({
  job,
  capabilities,
  serviceConfirmed
}: {
  job: PolymerMdJob;
  capabilities: PolymerMdCapabilities;
  serviceConfirmed: boolean;
}) {
  const stages = job.stage_runs.filter(s => s.status === "completed" && (/^eq[123]$/.test(s.stage) || /^additional_\d+$/.test(s.stage) || s.stage === "tc"));
  const [restored] = useState(() => readPolymerMdResultView(job)?.trajectory ?? null);
  const [stageId, setStageId] = useState(() => restored?.stageId ?? stages.find(s => /^eq\d+$/.test(s.stage))?.stage_run_id ?? stages[0]?.stage_run_id ?? "");
  const [offset, setOffset] = useState(restored?.offset ?? 0),
    [trajectory, setTrajectory] = useState<PolymerMdTrajectory | null>(restored?.trajectory ?? null);
  const [error, setError] = useState<string | null>(null),
    [loading, setLoading] = useState(false);
  const [selections, setSelections] = useState<MdAtomSelectionSlots>(restored?.selections ?? [null, null]);
  const [distance, setDistance] = useState<MdDemoAtomDistanceResponse | null>(restored?.distance ?? null),
    [distanceError, setDistanceError] = useState<string | null>(null),
    [distanceLoading, setDistanceLoading] = useState(false);
  const [distanceSampling, setDistanceSampling] = useState<unknown>(restored?.distanceSampling ?? null);
  const [distanceNotice, setDistanceNotice] = useState<string | null>(null), [distanceStarted, setDistanceStarted] = useState(0);
  const distanceNow = usePolymerMdClock(distanceLoading);
  const [pbc, setPbc] = useState(restored?.pbc ?? true);
  const identity = useRef(getSessionEpoch()), trajectoryAbort = useRef<AbortController | null>(null);
  const sourceIdentity = polymerMdTrajectoryIdentity(job, stageId);
  const displayedIdentity = useRef(JSON.stringify([sourceIdentity, offset]));
  const loadedIdentity = useRef(restored?.sourceIdentity ?? null);
  const analysisHealthy = !capabilities.analysis || ["ready", "busy"].includes(capabilities.analysis.status);
  const trajectoryReady = identity.current === getSessionEpoch() && serviceConfirmed && analysisHealthy && capabilities.trajectory_available;
  const distanceReady = identity.current === getSessionEpoch() && serviceConfirmed && analysisHealthy && capabilities.distance_available;
  const distanceAbort = useRef<AbortController | null>(null),
    generation = useRef(0);
  const clearDistance = () => {
    ++generation.current;
    distanceAbort.current?.abort();
    distanceAbort.current = null;
    setDistance(null);
    setDistanceSampling(null);
    setDistanceError(null);
    setDistanceNotice(null);
    setDistanceLoading(false);
  };
  function retireUnavailableView(error: unknown, operation: "轨迹读取" | "距离分析") {
    if (!(error instanceof Error) || !("status" in error) || ![403, 404].includes(Number(error.status))) return false;
    loadedIdentity.current = null;
    trajectoryAbort.current?.abort();
    clearDistance();
    clearPolymerMdTrajectoryView(job.job_id, identity.current);
    setTrajectory(null); setSelections([null, null]);
    setError(polymerMdReadError(error, operation));
    return true;
  }
  useEffect(() => onSessionRetired(() => {
    trajectoryAbort.current?.abort();
    clearDistance();
    setTrajectory(null); setSelections([null, null]); setError(null); setLoading(false);
  }), []);
  useEffect(() => {
    if (!stages.some(s => s.stage_run_id === stageId)) {
      setStageId(stages[0]?.stage_run_id ?? "");
      setOffset(0);
    }
  }, [stageId, stages]);
  useEffect(() => {
    const next = JSON.stringify([sourceIdentity, offset]);
    if (displayedIdentity.current === next) return;
    displayedIdentity.current = next;
    loadedIdentity.current = null;
    clearPolymerMdTrajectoryView(job.job_id, identity.current);
    setTrajectory(null);
    setError(null);
    setSelections([null, null]);
    clearDistance();
    // Scientific identity changes retire selections; service observations do not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.job_id, stageId, offset, sourceIdentity]);
  useEffect(() => () => { ++generation.current; distanceAbort.current?.abort(); distanceAbort.current = null; }, []);
  useEffect(() => {
    if (trajectory && sourceIdentity && loadedIdentity.current === sourceIdentity) savePolymerMdTrajectoryView(job, {
      sourceIdentity, stageId, offset, trajectory, selections, pbc, distance, distanceSampling
    }, identity.current);
  }, [job, sourceIdentity, stageId, offset, trajectory, selections, pbc, distance, distanceSampling]);
  useEffect(() => {
    if (!stageId || !sourceIdentity || !trajectoryReady) { setLoading(false); return; }
    if (loadedIdentity.current === sourceIdentity && trajectory?.job_id === job.job_id && trajectory.stage_run_id === stageId && trajectory.frames[0]?.frame_index === offset) return;
    const c = new AbortController(),
      epoch = getSessionEpoch();
    let disposed = false;
    trajectoryAbort.current = c;
    setLoading(true);
    setError(null);
    void boundedPolymerMdRead(c, POLYMER_MD_ANALYSIS_TIMEOUT_MS, signal => fetchPolymerMdTrajectory(job.job_id, stageId, offset, signal)).then(value => {
      if (c.signal.aborted || epoch !== getSessionEpoch()) return;
      if (value.job_id !== job.job_id || value.stage_run_id !== stageId) throw new Error("轨迹与当前任务阶段不匹配，请刷新。");
      loadedIdentity.current = sourceIdentity;
      setTrajectory(value);
      setError(null);
    }).catch(e => {
      if (!disposed && epoch === getSessionEpoch() && (!c.signal.aborted || e instanceof PolymerMdAnalysisTimeout)) {
        if (!retireUnavailableView(e, "轨迹读取")) setError(polymerMdReadError(e, "轨迹读取"));
      }
    }).finally(() => {
      if (!disposed && epoch === getSessionEpoch()) setLoading(false);
    });
    return () => {
      disposed = true;
      c.abort();
      if (trajectoryAbort.current === c) trajectoryAbort.current = null;
    };
    // Only a selected scientific identity or frame change retires its coordinates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.job_id, stageId, offset, trajectoryReady, sourceIdentity]);
  const frame = trajectory?.frames[0];
  function stopWaiting() {
    ++generation.current;
    distanceAbort.current?.abort();
    distanceAbort.current = null;
    setDistanceLoading(false);
    setDistanceNotice("已停止等待本次分析；后台可能仍在处理，请稍后再试。");
  }
  async function calculate() {
    const [firstAtom, secondAtom] = selections;
    if (!frame || !trajectory || !firstAtom || !secondAtom || !distanceReady || distanceAbort.current) return;
    const c = new AbortController();
    distanceAbort.current = c;
    const revision = ++generation.current,
      epoch = getSessionEpoch();
    setDistanceLoading(true);
    setDistanceStarted(Date.now());
    setDistanceError(null);
    setDistanceNotice(null);
    try {
      const d: PolymerMdDistance = await boundedPolymerMdRead(c, POLYMER_MD_ANALYSIS_TIMEOUT_MS, signal => calculatePolymerMdDistance(job.job_id, stageId, {
        atom_id_1: firstAtom.atom_id,
        atom_id_2: secondAtom.atom_id,
        frame_count: trajectory.total_frames,
        use_pbc: pbc
      }, signal));
      if (c.signal.aborted || revision !== generation.current || epoch !== getSessionEpoch()) return;
      if (d.job_id !== job.job_id || d.stage_run_id !== stageId || d.distance.length !== d.time_ps.length || d.distance.some(v => !Number.isFinite(v))) throw new Error("距离结果与当前阶段不匹配或数据不完整。");
      if (!d.distance.length) throw new Error("当前阶段没有可分析的距离样本。");
      setDistanceSampling(d.sampling ?? null);
      setDistance({
        atom_1: d.atom_1 ?? firstAtom,
        atom_2: d.atom_2 ?? secondAtom,
        frames: d.frames,
        time_ps: d.time_ps,
        distance: d.distance,
        series: {
          key: "atom_distance",
          label: "原子对距离",
          unit: d.unit ?? "A",
          points: d.distance.map((value, i) => ({
            time_ps: d.time_ps[i],
            value
          }))
        },
        stats: {
          n_atoms: frame.points.length,
          n_frames: d.distance.length,
          source_n_frames: trajectory?.total_frames ?? d.distance.length,
          n_chains: new Set(frame.points.map(p => p.chain_id)).size,
          min_distance: Math.min(...d.distance),
          max_distance: Math.max(...d.distance),
          ...d.stats,
          use_pbc: pbc
        }
      });
    } catch (e) {
      if (revision === generation.current && epoch === getSessionEpoch() && (!c.signal.aborted || e instanceof PolymerMdAnalysisTimeout)) {
        if (!retireUnavailableView(e, "距离分析")) setDistanceError(polymerMdReadError(e, "距离分析"));
      }
    } finally {
      if (distanceAbort.current === c && revision === generation.current && epoch === getSessionEpoch()) {
        distanceAbort.current = null;
        setDistanceLoading(false);
      }
    }
  }
  if (!frame && !trajectoryReady) return <div className="np-pmd-result-empty"><Atom aria-hidden="true" /><h3>轨迹暂不可浏览</h3><p className="np-polymer-md-muted">当前服务尚未提供轨迹浏览；已有产物可在下方下载。</p></div>;
  if (!stages.length) return <div className="np-pmd-result-empty"><Atom aria-hidden="true" /><h3>等待已完成的平衡轨迹</h3><p>平衡阶段完成后可浏览轨迹。</p></div>;
  return <section aria-label="正式任务轨迹"><div className="np-polymer-md-actions np-pmd-trajectory-controls"><PolymerMdSelect label="轨迹阶段" value={stageId} options={stages.map(s => ({ value: s.stage_run_id, label: `${stageLabel(s.stage)} · 第 ${s.attempt} 次` }))} onChange={stageRunId => {
          clearDistance();
          setStageId(stageRunId);
          setOffset(0);
        }} /><div className="np-pmd-frame-navigation" role="group" aria-label="轨迹翻帧"><button type="button" disabled={loading || !trajectoryReady || offset <= 0} onClick={() => setOffset(v => v - 1)}>上一帧</button><span className="np-pmd-frame-position"><PolymerMdNumericText>{`第 ${offset + 1} 帧${trajectory ? ` / ${trajectory.total_frames} 帧` : ""}`}</PolymerMdNumericText></span><button type="button" disabled={loading || !trajectoryReady || !trajectory || offset + 1 >= trajectory.total_frames} onClick={() => setOffset(v => v + 1)}>下一帧</button></div></div>{loading ? <div className="np-pmd-trajectory-loading" role="status"><LoaderCircle className="np-sw-spin" aria-hidden="true" /><strong>正在读取当前任务的轨迹…</strong><p>轨迹较大时，请稍候。</p></div> : null}{error ? <p role="alert">{error}</p> : null}{frame ? <><div className="np-pmd-trajectory-meta"><p className="np-pmd-trajectory-caption"><PolymerMdNumericText>{`采样坐标：${frame.points.length.toLocaleString()} 个原子 · ${frame.time_ps} ps`}</PolymerMdNumericText></p><label className="np-pmd-trajectory-pbc"><input type="checkbox" checked={pbc} onChange={e => {
          setPbc(e.target.checked);
          clearDistance();
        }} /> 距离使用周期边界</label></div>{!distanceReady ? <p className="np-pmd-analysis-availability" role="status">{!serviceConfirmed ? "服务状态待确认，已保留当前轨迹与原子选择；确认后可继续分析。" : "分析服务暂不可用，已保留当前轨迹与原子选择。"}</p> : capabilities.analysis?.status === "busy" ? <p className="np-pmd-analysis-availability" role="status">分析服务繁忙，新请求按可用名额受理。</p> : null}{distance ? <DistanceSampling value={distanceSampling} /> : null}<MdTrajectoryExplorer key={`${job.job_id}:${stageId}`} points={frame.points} timePs={frame.time_ps} stageLabel={stageLabel(stages.find(s => s.stage_run_id === stageId)?.stage ?? "")} frameLabel={`第 ${frame.frame_index + 1} 帧`} distanceUsePbc={pbc} selections={selections} distance={distance} distanceLoading={distanceLoading} distanceError={distanceError} distanceNotice={distanceNotice} distanceElapsedSeconds={Math.max(0, Math.floor((distanceNow - distanceStarted) / 1000))} onCancelDistance={stopWaiting} onSelectionChange={s => {
        setSelections(s);
        clearDistance();
      }} distanceEnabled={distanceReady} onCalculate={() => {
        if (distanceReady) void calculate();
      }} onClear={() => {
        setSelections([null, null]);
        clearDistance();
      }} /></> : null}</section>;
}
type ResultTab = PolymerMdResultTab;
const RESULT_TABS = [
  { id: "overview", label: "概览", description: "任务快照与科学指标", icon: Gauge },
  { id: "curves", label: "曲线", description: "真实采样与分析曲线", icon: BarChart3 },
  { id: "conformation", label: "构象", description: "阶段轨迹与原子距离", icon: Atom },
  { id: "files", label: "输出文件", description: "计算产物下载与清理", icon: Files }
] as const;

export function PolymerMdResults({
  job,
  capabilities,
  serviceCheck,
  busy,
  onCancel,
  onClear,
  onDelete
}: {
  job: PolymerMdJob;
  capabilities: PolymerMdCapabilities | null;
  serviceCheck?: PolymerMdServiceCheck;
  busy: string | null;
  onCancel: () => void;
  onClear: () => void;
  onDelete: () => void;
}) {
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<{ kind: "single" | "all"; artifactId?: string; done: number; total: number } | null>(null);
  const downloadOperation = useRef<{ controller: AbortController; jobId: string; epoch: number } | null>(null);
  const currentJobId = useRef(job.job_id);
  currentJobId.current = job.job_id;
  const [tab, setTab] = useState<ResultTab>(() => readPolymerMdResultView(job)?.tab ?? "overview");
  const now = usePolymerMdClock(tab === "overview" && polymerMdTimingIsLive(job));
  const resultId = useId();
  const tabId = (id: ResultTab) => `${resultId}-tab-${id}`;
  const panelId = `${resultId}-panel`;
  const identity = useRef(getSessionEpoch());
  useEffect(() => { savePolymerMdResultTab(job, tab, identity.current); }, [job, tab]);
  const isTerminal = terminalJob(job.status);
  const artifactsAvailable = job.artifact_state !== "deleted" && job.artifact_state !== "delete_requested";
  const artifacts = artifactsAvailable ? job.artifacts : [];
  const artifactStages = useMemo(() => new Map(job.stage_runs.map(stage => [stage.stage_run_id, stage])), [job.stage_runs]);
  const rows = useMemo(() => properties(job.results), [job.results]);
  const series = useMemo(() => [...seriesFrom(job.results), ...job.stage_runs.flatMap(s => seriesFrom(s.results))], [job.results, job.stage_runs]);
  useEffect(() => {
    const stop = () => {
      downloadOperation.current?.controller.abort();
      downloadOperation.current = null;
      setDownloading(null);
      setDownloadError(null);
    };
    stop();
    const unsubscribe = onSessionRetired(stop);
    return () => {
      unsubscribe();
      downloadOperation.current?.controller.abort();
      downloadOperation.current = null;
    };
  }, [job.job_id, artifactsAvailable]);
  function cancelDownload() {
    downloadOperation.current?.controller.abort();
    downloadOperation.current = null;
    setDownloading(null);
  }
  async function download(artifact?: PolymerMdJob["artifacts"][number]) {
    if (downloadOperation.current || busy || !artifacts.length || identity.current !== getSessionEpoch()) return;
    // A running job can publish further files while this fixed batch is saved.
    const snapshot = artifacts.map(item => ({ ...item }));
    const operation = { controller: new AbortController(), jobId: job.job_id, epoch: getSessionEpoch() };
    downloadOperation.current = operation;
    const current = () => downloadOperation.current === operation && !operation.controller.signal.aborted
      && operation.jobId === currentJobId.current && operation.epoch === getSessionEpoch();
    setDownloading({ kind: artifact ? "single" : "all", artifactId: artifact?.artifact_id, done: 0, total: artifact ? 1 : snapshot.length });
    setDownloadError(null);
    try {
      if (artifact) await downloadPolymerMdArtifact(operation.jobId, artifact.artifact_id, artifact.filename, artifact.size_bytes, operation.controller.signal);
      else await downloadPolymerMdArtifacts(operation.jobId, snapshot, (done, total) => {
        if (current()) setDownloading({ kind: "all", done, total });
      }, operation.controller.signal);
    } catch (e) {
      if (current() && !(e instanceof DOMException && e.name === "AbortError")) setDownloadError(e instanceof Error ? e.message : "下载失败。");
    } finally {
      if (current()) {
        downloadOperation.current = null;
        setDownloading(null);
      }
    }
  }
  return <section className="np-polymer-md-card np-pmd-results" aria-label="正式任务详情">
    <header className="np-pmd-job-header">
      <div className="np-pmd-job-header__identity"><span className="np-pmd-result-mark"><Gauge aria-hidden="true" /></span><div><h2>任务详情</h2><div className="np-pmd-result-id"><span>任务 ID</span><code className="np-polymer-md-id">{job.job_id}</code></div><p>结果对应当前任务的提交参数；修改配置不会改变这份结果。</p></div></div>
      <div className="np-polymer-md-actions np-pmd-job-header__actions">{!isTerminal
        ? <button type="button" disabled={Boolean(busy) || job.status === "cancel_requested"} onClick={onCancel}><Ban aria-hidden="true" />{job.status === "cancel_requested" ? "正在取消，等待进程退出" : "取消任务"}</button>
        : <button type="button" disabled={Boolean(busy) || Boolean(downloading)} onClick={onDelete}><Trash2 aria-hidden="true" />删除任务记录</button>}
      </div>
    </header>
    <div className="np-pmd-result-tabs" role="tablist" aria-label="聚合物 MD 结果视图" onKeyDown={event => {
      const delta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!delta && !["Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = RESULT_TABS.findIndex(item => item.id === tab);
      const next = event.key === "Home" ? RESULT_TABS[0] : event.key === "End" ? RESULT_TABS.at(-1)!
        : RESULT_TABS[(index + delta + RESULT_TABS.length) % RESULT_TABS.length];
      setTab(next.id);
      document.getElementById(tabId(next.id))?.focus();
    }}>
      {RESULT_TABS.map(({ id, label, description, icon: Icon }) => <button key={id} id={tabId(id)} type="button" role="tab" aria-label={label} aria-describedby={`${tabId(id)}-description`} aria-selected={tab === id} aria-controls={panelId} tabIndex={tab === id ? 0 : -1} className={tab === id ? "is-active" : undefined} onClick={() => setTab(id)}>
        <Icon aria-hidden="true" /><span><strong>{label}</strong><small id={`${tabId(id)}-description`}>{description}</small></span>
      </button>)}
    </div>
    <section id={panelId} role="tabpanel" aria-labelledby={tabId(tab)} className="np-pmd-result-panel">
      {tab === "overview" ? <>
        <dl className="np-polymer-md-summary"><div className="np-pmd-summary-status" data-status={job.status}><dt>执行状态</dt><dd>{STATUS_LABELS[job.status]}</dd></div><div className={`np-pmd-summary-quality is-${job.quality_status}`}><dt>科学质量</dt><dd>{QUALITY_LABELS[job.quality_status]}</dd></div><div><dt>当前阶段</dt><dd><span className="np-pmd-result-stage-chip">{stageLabel(job.stage)}</span></dd></div><div><dt>实际随机种子</dt><dd className={typeof job.scientific_request.seed === "number" ? "np-pmd-numeric-value" : undefined}>{job.scientific_request.seed ?? "待分配"}</dd></div></dl>
        <div className="np-pmd-result-time-panel"><PolymerMdJobTiming job={job} now={now} /></div>
        <PolymerMdRequestSummary request={job.scientific_request} />
        <FailureDiagnostic message={job.error_message} code={job.error_code} />
        {job.status === "completed" && job.quality_status !== "passed" ? <p className="np-polymer-md-notice">计算流程已结束，部分科学判据尚未通过。请按各项质量说明使用结果。</p> : null}
        <section className="np-pmd-overview-section" aria-label="阶段记录"><h3><Activity aria-hidden="true" />阶段记录</h3>
          {job.stage_runs.length ? <ol className="np-polymer-md-stages">{job.stage_runs.map(s => <li key={s.stage_run_id} data-status={s.status}><div className="np-pmd-stage-heading"><strong>{stageLabel(s.stage)} · 第 <span className="np-pmd-numeric-value">{s.attempt}</span> 次</strong><span className="np-pmd-stage-state">{({
            completed: "阶段完成", running: "运行中", pending: "待执行", failed: "失败", cancelled: "已取消"
          } as Record<string, string>)[s.status] ?? s.status}</span></div><span className={`np-pmd-stage-quality is-${s.quality_status}`}>阶段质量 · {QUALITY_LABELS[s.quality_status]}</span><StageProgress stage={s} /><StageLogs jobId={job.job_id} stage={s} available={job.artifact_state !== "deleted" && job.artifact_state !== "delete_requested"} />{s.errors.map((e, i) => <FailureDiagnostic key={i} message={e.message} code={e.code} />)}</li>)}</ol> : <p className="np-polymer-md-muted">尚无阶段执行记录。</p>}
        </section>
        <section className="np-pmd-overview-section" aria-label="汇总物性"><h3><Gauge aria-hidden="true" />科学指标</h3>
          {rows.length ? <div className="np-polymer-md-table-wrap np-pmd-property-table"><table><thead><tr><th>性质</th><th>数值</th><th>单位</th><th>质量说明</th></tr></thead><tbody>{rows.map(p => <tr key={p.key}><th scope="row">{p.label}</th><td className={`np-pmd-property-value${p.value !== null ? " np-pmd-numeric-value" : " is-unavailable"}`}>{p.value === null ? "不可用" : `${numberText(p.value)}${p.uncertainty !== undefined ? ` ± ${numberText(p.uncertainty)}` : ""}`}</td><td className="np-pmd-property-unit">{p.unit}</td><td className="np-pmd-property-quality"><span className={`np-pmd-property-quality-label is-${p.quality ?? "not_evaluated"}`} title={qualityCodeTitle(p.reason)}>{p.reason ? polymerMdQualityReason(p.reason) : (p.quality ? QUALITY_LABELS[p.quality as keyof typeof QUALITY_LABELS] ?? p.quality : "见阶段质量判据")}</span>{p.method ? <small className="np-polymer-md-method" title={safeCode(p.method)}>{polymerMdQualityMethod(p.method)}</small> : null}{p.source || p.range ? <small className="np-polymer-md-method">{[p.source, p.range ? `分析区间 ${p.range}` : undefined].filter(Boolean).join(" · ")}</small> : null}</td></tr>)}</tbody></table></div>
            : <div className="np-pmd-result-empty"><Gauge aria-hidden="true" /><h3>物性结果尚未发布</h3><p>当前尚无已发布的基础物性结果。</p></div>}
          {job.stage_runs.filter(s => properties(s.results).length > 0).map(s => <details className="np-pmd-stage-properties" key={s.stage_run_id}><summary>{stageLabel(s.stage)}的阶段物性</summary><dl>{properties(s.results).map(p => <div key={p.key}><dt>{p.label}</dt><dd><span className={p.value !== null ? "np-pmd-numeric-value" : undefined}>{p.value === null ? "不可用" : numberText(p.value)}</span> <span>{p.unit}</span>{p.reason ? <small title={qualityCodeTitle(p.reason)}> · {polymerMdQualityReason(p.reason)}</small> : null}</dd></div>)}</dl></details>)}
        </section>
        <RunEvidence job={job} rows={rows} />
      </> : null}
      {tab === "curves" ? <section className="np-pmd-curves-section"><header className="np-pmd-result-section-heading"><div><h3><BarChart3 aria-hidden="true" />采样与分析曲线</h3><p>按各曲线实际返回的时间、位置或温度坐标展示。</p></div></header>{series.length ? <div className="np-pmd-curves">{series.map((s, i) => <div className="np-polymer-md-chart" key={`${s.key}:${i}`}><h3>{s.label}</h3><MdSeriesChart series={s} xAxisLabel={s.spatial ? "位置 (Å)" : s.temperature ? "温度 (K)" : undefined} xVariable={s.spatial ? "位置" : s.temperature ? "温度" : undefined} /></div>)}</div>
        : <div className="np-pmd-result-empty"><BarChart3 aria-hidden="true" /><h3>尚无可用曲线</h3><p>当前任务尚未发布真实采样曲线；已有数值与质量说明可在概览中查看。</p></div>}</section> : null}
      {tab === "conformation" ? <section className="np-pmd-conformation-section"><header className="np-pmd-result-section-heading"><div><h3><Atom aria-hidden="true" />阶段构象与原子距离</h3><p>选择已完成阶段的真实轨迹，按帧查看构象与周期边界下的距离。</p></div></header>{capabilities ? <Trajectory key={`${job.job_id}:${artifactsAvailable ? "available" : "deleted"}`} job={job} serviceConfirmed={serviceCheck === undefined || serviceCheck === "confirmed"} capabilities={job.artifact_state === "deleted" || job.artifact_state === "delete_requested" ? {
        ...capabilities, trajectory_available: false, distance_available: false
      } : capabilities} /> : <div className="np-pmd-result-empty"><Atom aria-hidden="true" /><h3>正在读取轨迹服务能力</h3><p>服务能力确认后可查看已完成阶段的真实轨迹。</p></div>}</section> : null}
      {tab === "files" ? <section className="np-polymer-md-artifacts"><header className="np-pmd-result-section-heading"><div><h3><Files aria-hidden="true" />计算产物</h3><p className="np-polymer-md-muted">清理只移除输出文件，保留任务记录与已汇总物性。下载和轨迹浏览需对应文件仍保留。</p></div><div className="np-polymer-md-actions np-pmd-artifact-actions"><button type="button" className="is-primary" disabled={Boolean(busy) || Boolean(downloading) || !artifacts.length} onClick={() => void download()}><Download aria-hidden="true" />下载所有产物</button><button type="button" className="np-pmd-artifact-clear" disabled={!isTerminal || Boolean(busy) || Boolean(downloading) || !artifacts.length} onClick={onClear}><Trash2 aria-hidden="true" />清理输出文件</button></div></header>
        {!isTerminal && artifacts.length ? <p className="np-pmd-artifact-current">任务仍在运行，本次下载仅包含当前已生成的 <span className="np-pmd-numeric-value">{artifacts.length}</span> 个产物。</p> : null}
        {downloading ? <div className="np-pmd-artifact-progress"><p role="status" aria-label="产物下载进度">{downloading.kind === "all" ? <><PolymerMdNumericText>{`已接收 ${downloading.done} / ${downloading.total} 个产物`}</PolymerMdNumericText><span> · 正在保存 ZIP…</span></> : "正在下载所选文件…"}</p><button type="button" onClick={cancelDownload}>取消下载</button></div> : null}
        {downloadError ? <p role="alert">{downloadError}</p> : null}
        {artifacts.length ? <ul>{artifacts.map(a => {
          const source = artifactStages.get(a.stage_run_id), originId = `${resultId}-artifact-origin-${encodeURIComponent(a.artifact_id)}`;
          return <li key={a.artifact_id}><span><code>{a.filename}</code><small id={originId} className="np-pmd-artifact-origin">来源：{source ? <>{stageLabel(source.stage)} · 第 <span className="np-pmd-numeric-value">{source.attempt}</span> 次</> : "阶段来源未记录"}</small><small><span className="np-pmd-numeric-value">{(a.size_bytes / 1024).toFixed(1)}</span> KB</small></span><button type="button" aria-label={`下载 ${a.filename}`} aria-describedby={originId} disabled={Boolean(busy) || Boolean(downloading)} onClick={() => void download(a)}><Download aria-hidden="true" /><span>{downloading?.artifactId === a.artifact_id ? "下载中…" : "下载"}</span></button></li>;
        })}</ul>
          : <div className="np-pmd-result-empty"><Files aria-hidden="true" /><h3>暂无可下载文件</h3><p>尚无可下载产物，或产物已清理。</p></div>}
      </section> : null}
    </section>
  </section>;
}
