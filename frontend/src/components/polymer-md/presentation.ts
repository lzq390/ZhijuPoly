import type { PolymerMdRequest } from "../../types/polymerMd";
import { polymerMdUserConfigurationSpec } from "./userConfiguration";

export const FORCEFIELD_LABELS = Object.fromEntries(["GAFF", "GAFF2", "GAFF2_mod", "Dreiding", "Dreiding_UT"].map(value => [value, value]));
export const NATIVE_CHOICE_FIELDS = polymerMdUserConfigurationSpec.fields.filter(field => field.section === "quantum");

/** Read the saved request, never today's defaults or runtime configuration. */
export function requestedCalculationDescription(request: PolymerMdRequest): string {
  const targets = request.native_configuration?.properties;
  if (!targets) return request.calculation === "basic_tc" ? "基础物性与 RNEMD（须通过平衡门）" : "基础物性";
  const extras = [targets.includes("thermal_conductivity") || targets.includes("thermal_diffusivity") ? "RNEMD 热导率" : null,
    targets.includes("refractive_index") ? "静态近似折射率" : null,
    targets.includes("thermal_diffusivity") ? "热扩散率" : null,
    targets.includes("tg") ? "玻璃化转变温度 Tg" : null,
    targets.some(key => key === "sp_total" || key === "sp_ced") ? "溶解度参数 SP / 内聚能密度 CED" : null].filter(Boolean);
  const base = targets.some(key => !key.startsWith("qm_") && !["thermal_conductivity", "thermal_diffusivity", "refractive_index", "tg", "sp_total", "sp_ced"].includes(key)) ? "基础物性" : null;
  return [base, ...extras, targets.some(key => key.startsWith("qm_")) ? "历史单体量化目标" : null].filter(Boolean).join("、") || "未提供可识别的计算目标";
}
