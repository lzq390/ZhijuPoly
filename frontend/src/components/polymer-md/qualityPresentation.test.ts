import { describe, expect, it } from "vitest";
import { polymerMdQualityCodeTitle, polymerMdQualityMethod, polymerMdQualityReason } from "./qualityPresentation";

describe("polymer MD scientific quality presentation", () => {
  it.each([
    ["TotEng", "总能量"], ["E_bond", "键能"], ["E_angle", "键角能"],
    ["E_dihed", "二面角能"], ["E_vdwl", "范德华能"], ["Density", "密度"]
  ])("describes unstable_%s as the five-block convergence gate", (key, label) => {
    const text = polymerMdQualityReason(`unstable_${key}`);
    expect(text).toContain(label);
    expect(text).toContain("5 个区块均值波动（标准差）超过稳定性上限");
    expect(text).toContain("科学收敛判据");
    expect(text).not.toMatch(/失败|崩溃|程序错误/);
  });

  it("explains each semicolon-separated reason and keeps historical Chinese help", () => {
    const text = polymerMdQualityReason(" unstable_Density ; temperature_out_of_tolerance；有效样本不足，请继续采样 ");
    expect(text).toContain("密度在生产段");
    expect(text).toContain("生产段平均温度偏离目标温度");
    expect(text).toContain("有效样本不足，请继续采样");
    expect(polymerMdQualityReason("密度/温度尚未稳定")).toBe("密度/温度尚未稳定");
  });

  it("groups consecutive stability reasons without losing fields or repeating the gate", () => {
    const text = polymerMdQualityReason("unstable_TotEng;unstable_E_bond;unstable_E_dihed;unstable_E_vdwl");
    expect(text).toBe("总能量、键能、二面角能、范德华能在生产段的 5 个区块均值波动（标准差）超过稳定性上限，尚未达到科学收敛判据。");
    expect(text.match(/5 个区块均值/g)).toHaveLength(1);
    expect(text).not.toContain("键角能");
  });

  it("keeps other causes in order between separate consecutive stability groups", () => {
    const text = polymerMdQualityReason("unstable_TotEng;unstable_E_bond;temperature_out_of_tolerance;unstable_Density;unstable_E_dihed");
    const first = text.indexOf("总能量、键能在生产段"), temperature = text.indexOf("生产段平均温度偏离目标温度"), last = text.indexOf("密度、二面角能在生产段");
    expect(first).toBe(0);
    expect(temperature).toBeGreaterThan(first);
    expect(last).toBeGreaterThan(temperature);
    expect(text.match(/5 个区块均值/g)).toHaveLength(2);
  });

  it.each([
    ["density_missing", "密度结果未返回"],
    ["Cp_quality_not_passed", "定压比热结果未通过全部质量检查"],
    ["static_dielectric_const_non_finite_or_non_positive", "静态介电常数结果不是有效正值"],
    ["thermal_conductivity_unit_mismatch", "热导率结果的单位与要求不匹配"]
  ])("explains the exact generated dependency %s", (code, expected) => {
    expect(polymerMdQualityReason(code)).toContain(expected);
    expect(polymerMdQualityReason(code)).toContain("暂不能用于该项派生估计");
  });

  it("does not attribute a global dependency warning to that property's own stability gate", () => {
    const text = polymerMdQualityReason("density_quality_not_passed");
    expect(text).toBe("密度结果未通过全部质量检查，暂不能用于该项派生估计。");
    expect(text).not.toMatch(/密度波动|超过|失败/);
  });

  it.each([
    "static_electronic_approximation;uncertainty_not_propagated",
    "electronic_correction_approximation;uncertainty_not_propagated",
    "derived_estimate;uncertainty_not_propagated_from_kappa_density_Cp",
    "cooling_rate_and_independent_seed_sensitivity_not_evaluated",
    "multicomponent_molecular_values_are_reported_per_component"
  ])("retains the meaning of informational warnings: %s", code => {
    const text = polymerMdQualityReason(code);
    expect(text).not.toMatch(/未识别|失败|未收敛|崩溃/);
    expect(text).toMatch(/近似|不确定度|推导|尚未评估|按组分/);
  });

  it.each([
    ["insufficient_production_samples", "生产段样本不足"],
    ["non_physical_fluctuation_estimate", "物理有效性"],
    ["missing_or_ambiguous_TU0_TU1_end_groups", "链首尾端基"],
    ["dipole_sampling_not_converged", "偶极涨落"],
    ["sp_ced_block_stability_not_confirmed", "内聚能密度"],
    ["Tg_transition_not_stable_across_time_blocks", "不同时间区块"],
    ["insufficient_RNEMD_production_temperature_profiles", "温度剖面不足"],
    ["job_cancelled", "任务已取消"],
    ["selected_property_not_produced", "尚未生成所选性质"]
  ])("projects each scientific family without inventing a cause: %s", (code, expected) => {
    expect(polymerMdQualityReason(code)).toContain(expected);
  });

  it("retains an unknown safe code using an explicitly neutral explanation", () => {
    expect(polymerMdQualityReason("future_quality_reason_v2")).toBe("未识别的质量原因（原代码：future_quality_reason_v2）");
    expect(polymerMdQualityReason("constructor")).toBe("未识别的质量原因（原代码：constructor）");
    expect(polymerMdQualityReason("  ")).toBe("未提供质量原因。");
    expect(polymerMdQualityReason(" ;； ")).toBe("未提供质量原因。");
  });

  it.each([
    "/private/CANARY/result.log", "file:///private/CANARY", "C:\\private\\CANARY", "../CANARY/result",
    "日志位于/private/CANARY", "日志位于/sensitive/CANARY/result", "password=CANARY", "Bearer CANARY",
    "Authorization: Bearer CANARY", "api_key: CANARY", "cookie=CANARY", "密码：CANARY", "future_reason\nCANARY", "a".repeat(64), `future_${"a".repeat(32)}`
  ])("does not expose paths or credentials in either projection: %s", value => {
    expect(polymerMdQualityReason(value)).toBe("质量原因包含不可展示的信息。");
    expect(polymerMdQualityMethod(value)).toBe("未提供可展示的计算方法");
    expect(polymerMdQualityCodeTitle(value)).toBeUndefined();
  });

  it("does not expose a single private path attached directly to a Chinese explanation", () => {
    expect(polymerMdQualityReason("运行输出位于/sensitive")).toBe("质量原因包含不可展示的信息。");
    expect(polymerMdQualityMethod("日志位于/sensitive")).toBe("未提供可展示的计算方法");
    expect(polymerMdQualityCodeTitle("日志位于/sensitive")).toBeUndefined();
  });

  it("keeps safe reasons visible while removing an unsafe segment", () => {
    const text = polymerMdQualityReason("unstable_Density;password=CANARY;insufficient_production_samples");
    expect(text).toContain("密度在生产段");
    expect(text).toContain("生产段样本不足");
    expect(text).toContain("不可展示");
    expect(text).not.toContain("CANARY");
    expect(polymerMdQualityCodeTitle("unstable_Density;password=CANARY;insufficient_production_samples")).toBeUndefined();
  });

  it.each([
    ["NPT_fluctuation_5_block_SE", "等温等压（NPT）系综涨落公式与五区块标准误差"],
    ["production_mean_with_5_block_SE", "生产段均值与五区块标准误差"],
    ["mass_weighted_chain_Rg_mean_5_block_SE", "质量加权链回转半径"],
    ["TU0_TU1_closest_atom_distance_squared_5_block_SE", "链首尾端基最近原子距离平方"],
    ["fixed_charge_dipole_fluctuation_5_block_SE", "偶极矩涨落"],
    ["Einstein_all_atom_MSD_com_removed_linear_fit", "去除整体质心运动"],
    ["kappa_over_density_Cp", "热导率除以密度与定压比热乘积"],
    ["RNEMD_Muller_Plathe_5_block_SE", "Müller–Plathe"],
    ["RadonPy_SPMD_total_group_group_5_block_SE", "内聚能密度分析"],
    ["RadonPy_EQMD_step_wise_corrected_density_two_line_5_block_SE", "密度双直线拟合"]
  ])("explains the implemented method %s", (code, expected) => {
    expect(polymerMdQualityMethod(code)).toContain(expected);
  });

  it("keeps unknown method identifiers and historical Chinese descriptions without guessing", () => {
    expect(polymerMdQualityMethod("RadonPy.QMw.wb97m-d3bj/6-311+G(2d,p)")).toBe("RadonPy.QMw.wb97m-d3bj/6-311+G(2d,p)");
    expect(polymerMdQualityMethod("量子化学方法 HF/6-31G(d)")).toBe("量子化学方法 HF/6-31G(d)");
    expect(polymerMdQualityMethod("wb97m-d3bj/6-31G(d,p); RESP HF/6-31G(d)")).toBe("wb97m-d3bj/6-31G(d,p); RESP HF/6-31G(d)");
    expect(polymerMdQualityMethod("future_method_v3")).toBe("future_method_v3");
    expect(polymerMdQualityMethod("历史生产段均值分析")).toBe("历史生产段均值分析");
    expect(polymerMdQualityMethod("constructor")).toBe("constructor");
  });

  it.each([
    "unstable_TotEng;unstable_E_bond;unstable_E_dihed;unstable_E_vdwl",
    "NPT_fluctuation_5_block_SE",
    "RadonPy.QMw.wb97m-d3bj/6-311+G(2d,p)",
    "B3LYP/def2-SVP",
    "wb97m-d3bj/6-31G(d,p); RESP HF/6-31G(d)",
    "future_method_v3"
  ])("keeps safe original technical identifiers available as titles: %s", value => {
    expect(polymerMdQualityCodeTitle(value)).toBe(value);
  });

  it.each([undefined, null, 1, {}, "", "  ", "历史中文说明", ";unstable_Density", "unstable_Density;", "foo<tag>", "x".repeat(501), "sensitive/CANARY", "stages/result.json"])("does not echo non-technical or unbounded title values: %s", value => {
    expect(polymerMdQualityCodeTitle(value)).toBeUndefined();
  });
});
