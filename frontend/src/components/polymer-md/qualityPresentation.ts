/** Presentation only. The stored quality status, measurements and codes stay authoritative. */
const hiddenReason = "质量原因包含不可展示的信息。";
const hiddenMethod = "未提供可展示的计算方法";

function privatePartText(value: string) {
  return /[\u0000-\u001f\u007f\\]/.test(value)
    || /(?:file|unix|https?):\/\//i.test(value)
    || /(?:^|[\s"'(=;:：])(?:\/|[A-Za-z]:[\\/]|~\/|\.{1,2}\/)/.test(value)
    || /\/(?:home|tmp|opt|data|root|private|etc|var|app|usr|mnt|run)\b/i.test(value)
    || /\/[^\s/]+\//.test(value)
    || /[\u3400-\u9fff]\/[A-Za-z0-9._~-]+/.test(value)
    || /(?:token|password|passwd|secret|cookie|authorization|bearer|api[-_ ]?key)\s*(?:[:=]|\s+\S)/i.test(value)
    || /(?:密码|口令|令牌|密钥|会话凭证)\s*[:=：]/.test(value)
    || /[0-9a-f]{32,}/i.test(value);
}
const privateText = (value: string) => value.split(/[;；]/).some(privatePartText);

/** Only bounded technical identifiers can be echoed in a tooltip. */
export function polymerMdQualityCodeTitle(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const text = value.trim();
  if (!text || text.length > 500 || !/^[A-Za-z][A-Za-z0-9_+.,;()=\- /]*$/.test(text)) return undefined;
  // Check each code separately: two valid method/basis pairs separated by a
  // semicolon are not a multi-component filesystem path.
  if (text.split(";").some(part => {
    const code = part.trim();
    if (!code || privateText(code)) return true;
    // A slash in a title is allowed for method/basis notation, never an
    // arbitrary relative path. Unknown method text can still be shown below.
    return code.includes("/") && !/^[A-Za-z][A-Za-z0-9_.+ -]*\/(?:[0-9][A-Za-z0-9+.,()-]*|(?:def2-|(?:aug-)?cc-|STO-)[A-Za-z0-9+.,()-]+)$/i.test(code);
  })) return undefined;
  return text;
}

const stabilityFields: Readonly<Record<string, string>> = {
  unstable_TotEng: "总能量", unstable_E_bond: "键能", unstable_E_angle: "键角能",
  unstable_E_dihed: "二面角能", unstable_E_vdwl: "范德华能", unstable_Density: "密度"
};

const reasonLabels: Readonly<Record<string, string>> = {
  insufficient_production_samples: "有效生产段样本不足，尚不能确认统计结果收敛。",
  missing_required_thermo_columns: "热力学记录缺少质量分析所需的数据列。",
  non_finite_production_samples: "生产段样本包含非有限数值，暂不能用于有效统计。",
  temperature_out_of_tolerance: "生产段平均温度偏离目标温度，超过允许偏差。",
  block_uncertainty_or_sampling_not_converged: "五区块误差或采样稳定性尚未达到收敛判据。",
  non_physical_fluctuation_estimate: "涨落公式得到的估计值未通过物理有效性检查。",
  long_time_diffusive_regime_not_confirmed: "尚未确认长时间扩散区间，扩散拟合或区块误差未满足判据。",
  insufficient_production_trajectory_frames: "有效生产段轨迹帧不足，尚不能确认构象统计结果收敛。",
  insufficient_or_invalid_production_trajectory: "生产段轨迹不足或数据无效，相关轨迹性质暂不可用。",
  missing_or_ambiguous_TU0_TU1_end_groups: "链首尾端基标记缺失或不唯一，暂不能确定端到端距离。",
  missing_invalid_or_non_neutral_partial_charges: "原子部分电荷缺失、无效或体系不满足电中性要求。",
  missing_or_non_positive_periodic_cell: "周期晶胞信息缺失或体积不是有效正值。",
  missing_observations: "缺少该项性质所需的观测数据。",
  structure_block_uncertainty_not_converged: "构象统计的五区块误差尚未达到收敛判据。",
  dipole_sampling_not_converged: "偶极涨落采样误差尚未达到收敛判据。",
  missing_dipole_observations: "缺少有效偶极观测数据，暂不能计算介电响应。",
  insufficient_RNEMD_steady_state_samples: "RNEMD 稳态生产段样本不足，尚不能给出可靠热导率。",
  insufficient_RNEMD_production_temperature_profiles: "RNEMD 生产段温度剖面不足，尚不能确认温度梯度。",
  non_finite_or_non_positive_heat_transport: "热通量、温度梯度或热导估计未通过有限正值检查。",
  temperature_gradient_not_stable_or_linear: "温度梯度的线性拟合或温度块合格比例尚未满足稳定性判据。",
  exchange_energy_not_linear: "交换能量随时间的线性拟合尚未满足判据。",
  temperature_gradients_not_opposed: "两侧拟合温度梯度未呈现预期的相反方向。",
  conductivity_block_uncertainty_not_converged: "热导率的五区块误差尚未达到收敛判据。",
  component_QM_or_actual_composition_missing_or_invalid: "组分量子化学数据或实际重复单元组成缺失或无效。",
  actual_repeat_unit_composition_missing_or_empty: "实际重复单元组成缺失或为空，暂不能进行组分加权。",
  qm_polarizability_missing_non_finite_or_non_positive: "量子化学极化率缺失或不是有效正值。",
  qm_mol_weight_missing_non_finite_or_non_positive: "用于光学估计的分子量缺失或不是有效正值。",
  lorentz_lorenz_phi_outside_open_interval_0_1: "Lorentz–Lorenz 参数超出 0 到 1 的开区间，折射率估计不适用。",
  refractive_index_non_finite_or_non_positive: "折射率估计未通过有限正值检查。",
  refractive_index_unavailable: "缺少可用折射率，暂不能进行电子极化修正。",
  electronic_corrected_dielectric_non_finite_or_non_positive: "电子极化修正后的介电常数未通过有限正值检查。",
  density_Cp_denominator_non_finite_or_non_positive: "密度与定压比热组成的分母不是有效正值，暂不能推导热扩散率。",
  thermal_diffusivity_non_finite_or_non_positive: "热扩散率估计未通过有限正值检查。",
  static_electronic_approximation: "该值采用静态电子极化近似。",
  electronic_correction_approximation: "该值采用电子极化修正近似。",
  uncertainty_not_propagated: "尚未传播输入量的全部不确定度。",
  derived_estimate: "该值由已测得的依赖性质推导。",
  uncertainty_not_propagated_from_kappa_density_Cp: "尚未传播热导率、密度与定压比热的全部不确定度。",
  insufficient_SP_production_samples: "溶解度参数分析的有效生产段样本不足。",
  non_finite_or_non_positive_SP_observations: "溶解度参数分析的观测数据包含非有限值或无效非正值。",
  SP_sampling_temperature_not_reached: "溶解度参数采样的平均温度尚未达到目标温度允许范围。",
  cohesive_energy_density_non_finite_or_non_positive: "内聚能密度未通过有限正值检查。",
  sp_total_block_stability_not_confirmed: "溶解度参数的五区块误差或区块间变化尚未满足稳定性判据。",
  sp_ced_block_stability_not_confirmed: "内聚能密度的五区块误差或区块间变化尚未满足稳定性判据。",
  native_step_wise_run_block_count_mismatch: "逐温度平台运行的记录区块数与既定协议不一致。",
  missing_Tg_plateau_observations: "玻璃化转变分析缺少温度平台所需的观测数据。",
  insufficient_or_invalid_Tg_plateau_samples: "玻璃化转变分析的温度平台样本不足或数据无效。",
  Tg_plateau_temperature_not_reached: "温度平台的平均温度尚未达到目标温度允许范围。",
  insufficient_Tg_plateau_time_blocks: "温度平台的有效时间区块不足，暂不能估计区块误差。",
  no_supported_in_range_density_slope_transition: "当前温度范围内未找到满足判据的密度斜率转变。",
  Tg_transition_not_stable_across_time_blocks: "不同时间区块的密度斜率转变尚未保持稳定。",
  Tg_block_uncertainty_too_large: "玻璃化转变温度的区块误差超过允许上限。",
  cooling_rate_and_independent_seed_sensitivity_not_evaluated: "尚未评估冷却速率与独立随机种子对玻璃化转变估计的影响。",
  equilibrium_not_converged: "平衡判据尚未通过，依赖已平衡体系的追加性质尚未生成。",
  tc_ineligible: "当前体系尚未满足热导率计算的前置质量判据。",
  selected_property_not_produced: "本次执行尚未生成所选性质的有效结果。",
  job_cancelled: "任务已取消，尚未生成的性质没有有效结果。",
  job_failed: "任务执行失败，尚未生成的性质没有有效结果。",
  non_finite_or_missing: "该项结果缺失或不是有限数值。",
  multicomponent_molecular_values_are_reported_per_component: "多组分分子性质按组分分别报告，未定义统一的体系标量。",
  charge_model_does_not_use_QM_method_or_basis: "当前电荷模型不使用量子化学方法或基组。",
  dependent_molecular_calculation_not_requested: "本次未请求依赖的分子计算。",
  native_classical_charge_assignment_skips_terminal_QM_optimization: "原生经典电荷赋值流程不执行端基量子化学优化。"
};

const dependencies: Readonly<Record<string, string>> = {
  density: "密度", Cp: "定压比热", static_dielectric_const: "静态介电常数", thermal_conductivity: "热导率"
};
const dependencyReasons: Readonly<Record<string, string>> = {
  missing: "结果未返回，暂不能用于该项派生估计。",
  quality_not_passed: "结果未通过全部质量检查，暂不能用于该项派生估计。",
  non_finite_or_non_positive: "结果不是有效正值，暂不能用于该项派生估计。",
  unit_mismatch: "结果的单位与要求不匹配，暂不能用于该项派生估计。"
};
const lookup = (mapping: Readonly<Record<string, string>>, key: string) => Object.hasOwn(mapping, key) ? mapping[key] : undefined;
const stabilityReason = (fields: string[]) => `${fields.join("、")}在生产段的 5 个区块均值波动（标准差）超过稳定性上限，尚未达到科学收敛判据。`;

function reasonPart(value: string) {
  if (value.length > 500 || privateText(value) || /\/[A-Za-z0-9._~-]+/.test(value)) return hiddenReason;
  // Existing human explanations remain readable; never infer a scientific cause from prose.
  if (/[\u3400-\u9fff]/.test(value)) return value;
  const field = lookup(stabilityFields, value);
  if (field) return stabilityReason([field]);
  const label = lookup(reasonLabels, value);
  if (label) return label;
  const dependency = /^(density|Cp|static_dielectric_const|thermal_conductivity)_(missing|quality_not_passed|non_finite_or_non_positive|unit_mismatch)$/.exec(value);
  if (dependency) return `${dependencies[dependency[1]]}${dependencyReasons[dependency[2]]}`;
  if (!/^[A-Za-z][A-Za-z0-9_.+(),= -]*$/.test(value)) return hiddenReason;
  return `未识别的质量原因（原代码：${value}）`;
}

export function polymerMdQualityReason(value: string): string {
  if (!value.trim()) return "未提供质量原因。";
  if (value.length > 8192) return hiddenReason;
  const parts = value.split(/[;；]/).map(part => part.trim()).filter(Boolean);
  const explanations: string[] = [];
  for (let index = 0; index < parts.length;) {
    const fields: string[] = [];
    while (index < parts.length) {
      const field = lookup(stabilityFields, parts[index]);
      if (!field) break;
      fields.push(field);
      index++;
    }
    if (fields.length) explanations.push(stabilityReason(fields));
    else explanations.push(reasonPart(parts[index++]));
  }
  return explanations.join("；") || "未提供质量原因。";
}

const methodLabels: Readonly<Record<string, string>> = {
  production_mean: "生产段观测均值",
  production_mean_with_5_block_SE: "生产段均值与五区块标准误差",
  NPT_fluctuation: "等温等压（NPT）系综涨落公式",
  NPT_fluctuation_5_block_SE: "等温等压（NPT）系综涨落公式与五区块标准误差",
  production_trajectory: "生产段轨迹统计",
  production_trajectory_analysis: "生产段轨迹分析",
  mass_weighted_chain_Rg_mean_5_block_SE: "质量加权链回转半径均值与五区块标准误差",
  TU0_TU1_closest_atom_distance_squared_5_block_SE: "链首尾端基最近原子距离平方与五区块标准误差",
  residue_director_nematic_tensor_5_block_SE: "基于残基取向张量的向列序参数与五区块标准误差",
  fixed_charge_dipole_fluctuation: "固定电荷模型的偶极矩涨落公式",
  fixed_charge_dipole_fluctuation_5_block_SE: "固定电荷模型的偶极矩涨落公式与五区块标准误差",
  Einstein_all_atom_MSD_com_removed_linear_fit: "去除整体质心运动后，以全原子均方位移线性拟合的 Einstein 扩散估计",
  static_Lorentz_Lorenz_QM_polarizability: "量子化学静态极化率与 MD 密度的 Lorentz–Lorenz 折射率估计",
  "radonpy.core.calc.refractive_index": "RadonPy 基于极化率与密度的 Lorentz–Lorenz 折射率估计",
  epsilon_fixed_charge_plus_n_squared_minus_1: "固定电荷介电常数加上折射率平方减一的电子极化修正",
  kappa_over_density_Cp: "热导率除以密度与定压比热乘积的热扩散率推导",
  "radonpy.core.calc.thermal_diffusivity": "RadonPy 基于热导率、密度与定压比热的热扩散率推导",
  RNEMD_Muller_Plathe: "Müller–Plathe 反向非平衡分子动力学热导分析",
  RNEMD_Muller_Plathe_5_block_SE: "Müller–Plathe 反向非平衡分子动力学热导分析与五区块标准误差",
  RadonPy_SPMD_total_group_group_5_block_SE: "RadonPy 分子间相互作用总能量的溶解度参数与内聚能密度分析（五区块标准误差）",
  native_SPMD_rerun_total_group_group_adapter: "原生 SPMD 轨迹重算的分子间相互作用总能量分析",
  RadonPy_EQMD_step_wise_corrected_density_two_line_5_block_SE: "RadonPy 逐温度平台密度双直线拟合的玻璃化转变估计（五区块标准误差）",
  native_EQMD_equilibration_step_wise_with_corrected_density_two_line_analysis: "原生 EQMD 逐温度平台平衡与密度双直线拟合分析",
  stepwise_density_bilinear_fit: "逐温度平台密度双直线拟合",
  selected_property_unavailable: "所选性质暂不可用的记录（尚无有效测量值）"
};

export function polymerMdQualityMethod(value: string): string {
  const text = value.trim();
  if (!text || text.length > 500 || privateText(text)) return hiddenMethod;
  return lookup(methodLabels, text) ?? text;
}
