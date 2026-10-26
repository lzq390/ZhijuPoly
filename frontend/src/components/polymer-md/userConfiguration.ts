import specification from "../../../../polymer_md/user_configuration.json";
import { getSession } from "../../auth/session";
import { POLYMER_MD_API_BASE } from "../../services/polymerMdApi";
import type { PolymerMdRequest } from "../../types/polymerMd";
import { POLYMER_MD_NATIVE_FIELDS, validPolymerMdNativeConfiguration } from "../../services/polymerMdNativeConfiguration";
import { POLYMER_MD_DEFAULTS } from "./config";
import { expandPolymerMdProperties, POLYMER_MD_THERMAL_CONDUCTIVITY_PROPERTY, POLYMER_MD_SOLUBILITY_PROPERTIES } from "./propertyGroups";

type SourceReference = { path: string; symbol: string; line: number };
export type PolymerMdUserOption = {
  value: string;
  label: string;
  description: string;
  scope: "molecular" | "bulk" | "model" | "diagnostic" | "mixed";
  current_execution: boolean;
  category?: string;
  category_label?: string;
  source: SourceReference;
};
export type PolymerMdUserField = {
  id: string;
  label: string;
  section: "model" | "quantum" | "properties";
  selection: "single" | "multiple";
  default: string | string[];
  description: string;
  source: SourceReference;
  options: PolymerMdUserOption[];
  element_basis_overrides?: Partial<Record<"Br" | "I", string>>;
};
export type PolymerMdUserConfiguration = Record<string, string | string[]>;
export const polymerMdUserConfigurationSpec = specification as {
  configuration_version: number;
  source_sha: string;
  description: string;
  fields: PolymerMdUserField[];
};
export const POLYMER_MD_USER_CONFIGURATION_KEY = "nexpoly:polymer-md-simulation:user-configuration:v1";

export function defaultPolymerMdUserConfiguration(): PolymerMdUserConfiguration {
  return Object.fromEntries(polymerMdUserConfigurationSpec.fields.map(field => [field.id,
    Array.isArray(field.default) ? [...field.default] : field.default]));
}

function scope() {
  const current = getSession();
  return { owner: current.authenticated ? current.user?.id ?? "unknown" : "guest",
    session: current.authenticated ? current.session_id ?? "unknown" : "guest",
    api: new URL(POLYMER_MD_API_BASE, window.location.href).href };
}
function sameScope(a: ReturnType<typeof scope>, b: ReturnType<typeof scope>) {
  return a?.owner === b.owner && a?.session === b.session && a?.api === b.api;
}
function isConfiguration(value: unknown): value is PolymerMdUserConfiguration {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.values(value).every(item => typeof item === "string"
      || Array.isArray(item) && item.every(entry => typeof entry === "string")));
}
export function readPolymerMdUserConfiguration(): PolymerMdUserConfiguration {
  try {
    const raw = window.sessionStorage.getItem(POLYMER_MD_USER_CONFIGURATION_KEY);
    if (!raw) return defaultPolymerMdUserConfiguration();
    const value = JSON.parse(raw);
    if (!sameScope(value.scope, scope())) return defaultPolymerMdUserConfiguration();
    if (!isConfiguration(value.configuration)) return { ...defaultPolymerMdUserConfiguration(), __draft_error: "配置草稿内容无法识别，请检查后恢复默认配置。" };
    // Preserve unknown options/version markers for explicit repair. They must
    // never silently become today's default method or forcefield.
    return value.configuration_version === polymerMdUserConfigurationSpec.configuration_version
      ? value.configuration
      : { ...value.configuration, __configuration_version: String(value.configuration_version) };
  } catch {
    return { ...defaultPolymerMdUserConfiguration(), __draft_error: "无法读取配置草稿，请检查浏览器存储后恢复默认配置。" };
  }
}
export function savePolymerMdUserConfiguration(configuration: PolymerMdUserConfiguration): void {
  const raw = JSON.stringify({ configuration_version: polymerMdUserConfigurationSpec.configuration_version,
    scope: scope(), configuration });
  try {
    window.sessionStorage.setItem(POLYMER_MD_USER_CONFIGURATION_KEY, raw);
    if (window.sessionStorage.getItem(POLYMER_MD_USER_CONFIGURATION_KEY) !== raw) throw new Error();
  } catch {
    throw new Error("当前选择尚未保存。请恢复此标签页的会话存储，再继续配置。");
  }
}

/** New requests use managed sampling defaults; saved pending requests stay frozen. */
export function fixedPolymerMdExecutionRequest(base: PolymerMdRequest): PolymerMdRequest {
  return { ...base, calculation: "basic",
    eq_ns: POLYMER_MD_DEFAULTS.eq_ns, additional_ns: POLYMER_MD_DEFAULTS.additional_ns,
    max_additional: POLYMER_MD_DEFAULTS.max_additional, sample_interval_ps: POLYMER_MD_DEFAULTS.sample_interval_ps,
    tc_ns: POLYMER_MD_DEFAULTS.tc_ns, tc_rep: POLYMER_MD_DEFAULTS.tc_rep, seed: null };
}
/** Compile new MD forms with shared base analysis and explicit additional work. */
export function mapPolymerMdUserConfiguration(configuration: PolymerMdUserConfiguration, base: PolymerMdRequest): {
  request: PolymerMdRequest | null;
  unmapped: string[];
  executedProperties: string[];
  protocolLabel: string;
} {
  const unmapped: string[] = [];
  const fields = polymerMdUserConfigurationSpec.fields;
  for (const key of Object.keys(configuration)) {
    if (key === "__configuration_version") unmapped.push(`旧版配置草稿（v${configuration[key]}）已保留，请核对选择后迁移到当前版本，或恢复默认参数。`);
    else if (key === "__draft_error") unmapped.push(String(configuration[key]));
    else if (!fields.some(field => field.id === key)) unmapped.push(`配置项 ${key} 需要迁移，原值已保留。`);
  }
  for (const field of fields) {
    const value = configuration[field.id];
    const values = Array.isArray(value) ? value : typeof value === "string" ? [value] : [];
    if (field.selection === "single" && (typeof value !== "string" || !value)
      || field.selection === "multiple" && !Array.isArray(value)) {
      unmapped.push(`请设置${field.label}。`);
      continue;
    }
    if (new Set(values).size !== values.length || values.some(item => !field.options.some(option => option.value === item))) {
      unmapped.push(`${field.label}包含尚未识别的选择：${values.join("、")}，原值已保留，请重新选择。`);
      continue;
    }
  }
  const properties = expandPolymerMdProperties(Array.isArray(configuration.properties) ? configuration.properties : []);
  if (properties.some(key => key.startsWith("qm_"))) unmapped.push("本模块仅提交聚合物 MD；此前保存的单体量化结果选择已保留，请先移除这些目标。");
  if (POLYMER_MD_SOLUBILITY_PROPERTIES.some(key => properties.includes(key)) && base.n_chain < 2)
    unmapped.push("溶解度参数与内聚能密度需要至少2条聚合物链；请增加总链数后提交。");
  const native = Object.fromEntries(POLYMER_MD_NATIVE_FIELDS.map(key => [key,
    key === "properties" ? properties : configuration[key]]));
  const request: PolymerMdRequest | null = !unmapped.length && validPolymerMdNativeConfiguration(native)
    ? { ...fixedPolymerMdExecutionRequest(base),
      forcefield: configuration.forcefield as PolymerMdRequest["forcefield"],
      calculation: properties.includes(POLYMER_MD_THERMAL_CONDUCTIVITY_PROPERTY) ? "basic_tc" : "basic",
      model_configuration: base.model_configuration ?? { version: 1, mode: "homopolymer",
        components: [{ id: "A", smiles: base.smiles.trim() }], chain_length: { mode: "atoms", value: base.n_atom } },
      smiles: base.smiles.trim(), native_configuration: native } : null;
  return { request, unmapped, executedProperties: request ? [...properties] : [], protocolLabel: "平台聚合物 MD 默认运行方案 v1" };
}
