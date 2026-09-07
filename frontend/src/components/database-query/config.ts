import type { SmilesLookupTable } from "../../types";

export type DatabaseQueryTableOption = {
  value: SmilesLookupTable;
  label: string;
  shortLabel: string;
  description: string;
  fields: string;
};

export const DATABASE_QUERY_TABLES: readonly DatabaseQueryTableOption[] = [
  {
    value: "polymers",
    label: "结构-性能库 / Polymers",
    shortLabel: "Polymers",
    description: "聚合物级记录，用于确认结构是否已进入主结构-性能库。",
    fields: "smiles, canonical_smiles"
  },
  {
    value: "properties",
    label: "结构-性能库 / Properties",
    shortLabel: "Properties",
    description: "性能行级记录，返回当前结构关联的每一条性能数据。",
    fields: "polymers.smiles, polymers.canonical_smiles"
  },
  {
    value: "pi_candidates",
    label: "PI 反向设计库",
    shortLabel: "PI Polymer",
    description: "PI 候选聚合物、单体与计算属性，用于检查反向设计候选空间。",
    fields: "polym, canonical_polym, mon1, mon2"
  }
] as const;

export const DATABASE_QUERY_TABLE_META = Object.fromEntries(
  DATABASE_QUERY_TABLES.map((option) => [option.value, option])
) as Record<SmilesLookupTable, DatabaseQueryTableOption>;

export const DATABASE_QUERY_FIELD_LABELS: Readonly<Record<string, string>> = {
  polymer_id: "Polymer ID",
  property_id: "Property ID",
  property_count: "性能条目",
  property_name: "性能名称",
  property_value_num: "数值",
  property_unit: "单位",
  label_source: "来源",
  pi_id: "PI ID",
  mon1: "单体 A",
  mon2: "单体 B",
  polym: "聚合物 SMILES",
  tg_celsius: "Tg (°C)",
  dielectric_const_dc: "介电常数 DC",
  static_dielectric_const: "静态介电常数",
  dipole_debye: "偶极矩",
  electrophilicity_index: "亲电指数",
  homo_lumo_gap_ev: "HOMO-LUMO Gap",
  hardness: "硬度",
  mulliken_electronegativity: "电负性",
  redox_window_v: "氧化还原窗口",
  linear_expansion: "线性膨胀",
  refractive_index: "折射率"
};

export function formatDatabaseLookupValue(value: string | number | boolean | null) {
  if (value === null || value === "") return "-";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") {
    return Number.isInteger(value)
      ? String(value)
      : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  }
  return value;
}
