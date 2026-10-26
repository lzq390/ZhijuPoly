import type { PolymerMdCapabilities, PolymerMdRequest, PolymerMdStatus } from "../../types/polymerMd";
import { POLYMER_MD_FORCEFIELDS, validPolymerMdModelRequest, validPolymerMdPostEquilibriumRequest, estimatePolymerMdRequestedAtoms } from "../../services/polymerMdNativeConfiguration";
export const POLYMER_MD_DEFAULTS: PolymerMdRequest = {
  smiles: "*CC*",
  temp: 300,
  pressure: 1,
  n_atom: 1000,
  n_chain: 10,
  forcefield: "GAFF2_mod",
  terminal_head: "CH3",
  terminal_tail: "CH3",
  tacticity: "atactic",
  calculation: "basic",
  eq_ns: 5,
  additional_ns: 5,
  max_additional: 2,
  sample_interval_ps: 1,
  tc_axis: "x",
  tc_ns: 1,
  tc_rep: 3,
  seed: null
};
export const POLYMER_MD_DRAFT_KEY = "nexpoly:polymer-md-simulation:draft:v1";
export const terminalJob = (status: PolymerMdStatus) => ["completed", "failed", "cancelled"].includes(status);
export const STATUS_LABELS: Record<PolymerMdStatus, string> = {
  pending: "等待受理",
  queued: "排队中",
  running: "运行中",
  completed: "执行完成",
  failed: "执行失败",
  cancel_requested: "正在取消",
  cancelled: "已取消"
};
export const QUALITY_LABELS = {
  not_evaluated: "尚未评估",
  passed: "质量通过",
  warning: "部分指标需关注",
  not_converged: "尚未收敛",
  failed: "质量未通过"
};
export const STAGE_LABELS: Record<string, string> = {
  queued: "等待执行",
  preparing: "准备计算",
  qm: "量子化学计算",
  terminal_qm: "端基量子化学",
  monomer_qm: "重复单元量子化学",
  building: "聚合物建模",
  build: "聚合物建模",
  eq1: "EQ1 松弛与装填",
  eq2: "EQ2 压缩与解压",
  eq3: "EQ3 平衡采样",
  eq: "平衡模拟",
  additional: "追加平衡",
  basic: "基础物性分析",
  analyze: "基础物性分析",
  tc: "RNEMD 热导率",
  sp: "SP采样与能量重算",
  sp_analyze: "SP与内聚能分析",
  tg: "高温平衡与分段降温",
  tg_analyze: "Tg拟合与质量评估",
  rnemd: "RNEMD 热导率",
  completed: "结果汇总完成"
};
export function stageLabel(stage: string) {
  if (/^additional_\d+$/.test(stage)) return `第 ${stage.split("_")[1]} 次追加平衡`;
  if (/^analyze_\d+$/.test(stage)) return stage === "analyze_0" ? "基础物性分析" : "追加平衡后的物性分析";
  if (stage === "tc_analyze") return "热导率质量评估";
  return STAGE_LABELS[stage] ?? (/^eq\d+$/.test(stage) ? `${stage.toUpperCase()} 追加平衡` : stage);
}

/** Conservative counts on the actual 1 fs EQ / 0.2 fs RNEMD step grids.
 * Endpoint output never substitutes for enough production observations.
 * These are admission checks, not a claim of equilibrium or independent data.
 */
export function polymerMdSamplingEstimate(r: PolymerMdRequest) {
  const intervalSteps = Number.isFinite(r.sample_interval_ps) ? Math.round(r.sample_interval_ps * 1000) : 0;
  const equilibrium = (ns: number) => {
    const steps = Number.isFinite(ns) ? Math.round(ns * 1_000_000) : 0;
    const usableSteps = Math.max(0, steps - 2_000_000);
    return { durationPs: steps / 1000, burnInPs: 2000, usablePs: usableSteps / 1000,
      estimatedSamples: intervalSteps > 0 ? Math.floor(usableSteps / intervalSteps) : 0, minimumSamples: 500 };
  };
  const tcSteps = Number.isFinite(r.tc_ns) ? Math.round(r.tc_ns * 5_000_000) : 0;
  const tcUsableSteps = Math.max(0, tcSteps - 4_000_000);
  return { equilibrium: equilibrium(r.eq_ns), additional: r.max_additional > 0 ? equilibrium(r.additional_ns) : null,
    thermal: r.calculation === "basic_tc" ? { durationPs: tcSteps / 5000, burnInPs: 800,
      usablePs: tcUsableSteps / 5000, estimatedProfiles: Math.floor(tcUsableSteps / 1000), minimumProfiles: 20 } : null };
}

export function validatePolymerMdRequest(r: PolymerMdRequest, c?: PolymerMdCapabilities | null, options: { samplingChecks?: boolean } = {}): Record<string, string> {
  const e: Record<string, string> = {};
  if (!r.smiles || r.smiles.length > 2048 || !/^[\x21-\x7e]+$/.test(r.smiles) || (r.smiles.match(/\*/g)?.length ?? 0) !== 2 || /[.,]/.test(r.smiles)) e.smiles = "请输入带两个 * 连接点的单一线性均聚物重复单元 SMILES。";
  const number = (key: keyof PolymerMdRequest, min: number, max: number, integer = false) => {
    const v = r[key];
    if (typeof v !== "number" || !Number.isFinite(v) || v < min || v > max || integer && !Number.isInteger(v)) e[key] = `请输入 ${min}–${max} 之间的${integer ? "整数" : "数值"}。`;
  };
  number("temp", 100, 1000);
  number("pressure", Number.MIN_VALUE, 1000);
  if (e.pressure) e.pressure = "目标压力应大于 0 且不超过 1000 atm。";
  number("n_atom", 100, 5000, true);
  number("n_chain", 1, 20, true);
  if (!validPolymerMdPostEquilibriumRequest(r)) e.n_chain = "溶解度参数与内聚能密度需要至少2条聚合物链。";
  if (!validPolymerMdModelRequest(r)) e.model_configuration = "请检查模型类型、各重复单元、链长、组成比例及嵌段顺序；顶层结构和链数须与模型一致。";
  number("eq_ns", .01, 20);
  number("additional_ns", .01, 20);
  number("max_additional", 0, 2, true);
  number("sample_interval_ps", .1, 10);
  if (r.calculation === "basic_tc") {
    number("tc_ns", .01, 10);
    number("tc_rep", 1, 5, true);
  }
  if (options.samplingChecks !== false) {
    const sampling = polymerMdSamplingEstimate(r);
    if (!e.sample_interval_ps) {
      const exactInterval = r.sample_interval_ps * 1000;
      const intervalSteps = Math.round(exactInterval);
      if (Math.abs(exactInterval - intervalSteps) > 1e-7) e.sample_interval_ps = "输出间隔须为 0.001 ps（1 fs）的整数倍，以匹配实际积分步长。";
      const checkStage = (key: "eq_ns" | "additional_ns", estimate: typeof sampling.equilibrium) => {
        if (e[key]) return;
        const exactSteps = r[key] * 1_000_000;
        const steps = Math.round(exactSteps);
        if (Math.abs(exactSteps - steps) > 1e-7 || steps % intervalSteps !== 0) {
          e[key] = "采样时长须与输出间隔整步对齐；请调整时长或输出间隔。";
        } else if (estimate.estimatedSamples < estimate.minimumSamples) {
          e[key] = `该段独立丢弃前 2 ns 后，保守估计约 ${estimate.estimatedSamples} 条有效记录，正式物性采样至少需要 ${estimate.minimumSamples} 条。请延长该段或缩短输出间隔；短程配置仅用于开发诊断。`;
        }
      };
      checkStage("eq_ns", sampling.equilibrium);
      if (sampling.additional) checkStage("additional_ns", sampling.additional);
      if (e.eq_ns || sampling.additional && e.additional_ns) e.sample_interval_ps ??= "输出间隔需与每段时长对齐，并保证丢弃瞬态后至少 500 条热力学记录；追加段独立检查。";
    }
    if (r.calculation === "basic_tc" && !e.tc_ns && sampling.thermal) {
      const exactSteps = r.tc_ns * 5_000_000;
      const steps = Math.round(exactSteps);
      if (Math.abs(exactSteps - steps) > 1e-7 || steps % 1000 !== 0) e.tc_ns = "RNEMD 时长须按固定 0.2 ps 输出间隔对齐。";
      else if (sampling.thermal.estimatedProfiles < sampling.thermal.minimumProfiles) e.tc_ns = "丢弃前 0.8 ns 后至少需要 20 个完整温度平均块；请延长 RNEMD 采样。默认使用 1 ns，样本数量达标仍需通过稳态与误差判据。";
    }
  }
  if (r.seed != null) number("seed", 1, 2147483647, true);
  const estimatedAtoms = estimatePolymerMdRequestedAtoms(r);
  if (estimatedAtoms !== null && estimatedAtoms > (c?.limits.max_total_atoms ?? 20000)) e.n_chain = "估算的基础体系超过原子数上限。";
  if (r.calculation === "basic_tc" && estimatedAtoms !== null && estimatedAtoms * r.tc_rep > (c?.limits.max_rnemd_atoms ?? 60000)) e.tc_rep = "复制后的热导体系超过原子数上限。";
  for (const [key, values] of [["terminal_head", c?.supported_terminals ?? ["CH3", "H", "OH"]], ["terminal_tail", c?.supported_terminals ?? ["CH3", "H", "OH"]], ["tacticity", c?.supported_tacticities ?? ["atactic", "isotactic", "syndiotactic"]], ["calculation", c?.supported_calculations ?? ["basic", "basic_tc"]], ["tc_axis", ["x", "y", "z"]]] as const) if (!(values as readonly string[]).includes(r[key])) e[key] = "当前协议不支持此选项。";
  if (!POLYMER_MD_FORCEFIELDS.includes(r.forcefield)
    || !(c?.supported_forcefields ?? POLYMER_MD_FORCEFIELDS).includes(r.forcefield)) e.forcefield = "当前协议不支持此力场。";
  return e;
}
export function readPolymerMdDraft(): PolymerMdRequest {
  try {
    const raw = JSON.parse(sessionStorage.getItem(POLYMER_MD_DRAFT_KEY) ?? "null");
    if (raw?.version !== 1 || !raw.request || typeof raw.request !== "object") return {
      ...POLYMER_MD_DEFAULTS
    };
    const r = {
      ...POLYMER_MD_DEFAULTS,
      ...raw.request
    };
    // Keep an existing range-valid draft even when the stricter formal
    // sampling checks now reject it. Show the new errors for explicit repair.
    return Object.keys(validatePolymerMdRequest(r, null, { samplingChecks: false })).length ? {
      ...POLYMER_MD_DEFAULTS
    } : r;
  } catch {
    return {
      ...POLYMER_MD_DEFAULTS
    };
  }
}
export function savePolymerMdDraft(request: PolymerMdRequest) {
  try {
    sessionStorage.setItem(POLYMER_MD_DRAFT_KEY, JSON.stringify({
      version: 1,
      request
    }));
  } catch {/* Private drafts must never block submission. */}
}
export function jobIdFromLocation(): string | null {
  const id = new URLSearchParams(window.location.search).get("job");
  return id && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(id) && !id.includes("..") ? id : null;
}
