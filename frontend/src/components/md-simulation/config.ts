import type { MdDemoRunRequest } from "../../types";

export const MD_DEMO_EXAMPLE_SMILES = "*C(=C(*)C(F)(F)F)c1ccc(CCCC)cc1";

export const MD_DEMO_FORCEFIELD_OPTIONS = [
  { value: "GAFF2_mod", label: "GAFF2_mod", hint: "示例结构推荐" },
  { value: "GAFF2", label: "GAFF2", hint: "通用有机分子" },
  { value: "OPLS-AA", label: "OPLS-AA", hint: "全原子力场" },
  { value: "CHARMM36", label: "CHARMM36", hint: "全原子力场" },
  { value: "PCFF", label: "PCFF", hint: "聚合物体系" },
] as const;

export const MD_DEMO_FALLBACK_REQUEST: MdDemoRunRequest = {
  smiles: MD_DEMO_EXAMPLE_SMILES,
  temperature: 300,
  pressure: 1,
  n_atom: 1000,
  n_chain: 10,
  forcefield: "GAFF2_mod"
};

export const MD_DEMO_PROGRESS_STEPS = [
  { label: "输入检查", detail: "检查结构与运行条件", threshold: 8 },
  { label: "体系建模", detail: "构建聚合物链与初始模拟盒", threshold: 28 },
  { label: "EQ1 初始平衡", detail: "进行短程松弛并记录初始轨迹", threshold: 48 },
  { label: "EQ2 密度收敛", detail: "进行压力耦合并稳定盒子尺寸", threshold: 68 },
  { label: "EQ3 生产采样", detail: "读取生产阶段的采样结果", threshold: 88 },
  { label: "结果汇总", detail: "整理轨迹与距离分析结果", threshold: 100 }
] as const;

export type MdSimulationField = keyof MdDemoRunRequest;
export type MdSimulationFormErrors = Partial<Record<MdSimulationField, string>>;

export function normalizeMdDemoRequest(request: MdDemoRunRequest): MdDemoRunRequest {
  return {
    smiles: request.smiles.trim(),
    temperature: request.temperature,
    pressure: request.pressure,
    n_atom: Math.round(request.n_atom),
    n_chain: Math.round(request.n_chain),
    forcefield: request.forcefield.trim()
  };
}

function integerError(value: number, min: number, max: number, label: string) {
  if (!Number.isFinite(value) || !Number.isInteger(value)) return `${label}必须填写整数。`;
  if (value < min || value > max) return `${label}应在 ${min}–${max} 之间。`;
  return null;
}

export function validateMdDemoRequest(request: MdDemoRunRequest): MdSimulationFormErrors {
  const errors: MdSimulationFormErrors = {};
  const smiles = request.smiles.trim();
  const forcefield = request.forcefield.trim();
  if (!smiles) errors.smiles = "请先输入 SMILES。";
  else if (smiles.length > 4000) errors.smiles = "SMILES 不能超过 4000 个字符。";
  if (!Number.isFinite(request.temperature) || request.temperature <= 0 || request.temperature > 5000) {
    errors.temperature = "温度应大于 0 且不超过 5000 K。";
  }
  if (!Number.isFinite(request.pressure) || request.pressure <= 0 || request.pressure > 100000) {
    errors.pressure = "压力应大于 0 且不超过 100000 atm。";
  }
  const atomError = integerError(request.n_atom, 100, 500000, "目标原子数");
  if (atomError) errors.n_atom = atomError;
  const chainError = integerError(request.n_chain, 1, 10000, "链数");
  if (chainError) errors.n_chain = chainError;
  if (!forcefield) errors.forcefield = "请填写力场名称。";
  else if (forcefield.length > 64) errors.forcefield = "力场名称不能超过 64 个字符。";
  return errors;
}

export function sameMdDemoRequest(left: MdDemoRunRequest | null, right: MdDemoRunRequest) {
  if (!left) return false;
  const normalized = normalizeMdDemoRequest(right);
  return (
    left.smiles === normalized.smiles &&
    left.temperature === normalized.temperature &&
    left.pressure === normalized.pressure &&
    left.n_atom === normalized.n_atom &&
    left.n_chain === normalized.n_chain &&
    left.forcefield === normalized.forcefield
  );
}

export function progressStepFor(value: number) {
  return MD_DEMO_PROGRESS_STEPS.find((step) => value <= step.threshold) ??
    MD_DEMO_PROGRESS_STEPS[MD_DEMO_PROGRESS_STEPS.length - 1];
}
