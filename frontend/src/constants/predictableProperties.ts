import type { PredictableProperty } from "../types";

export type PredictPropertyGroup = "thermal" | "mechanical" | "permeability";

export type PredictPropertyDefinition = {
  key: PredictableProperty;
  label: string;
  unit: string;
  shortLabel: string;
  group: PredictPropertyGroup;
};

export const PREDICT_PROPERTY_CATALOG = [
  {
    key: "Glass transition temperature",
    label: "玻璃化转变温度",
    unit: "°C",
    shortLabel: "Tg",
    group: "thermal"
  },
  {
    key: "Melting temperature",
    label: "熔融温度",
    unit: "°C",
    shortLabel: "Tm",
    group: "thermal"
  },
  {
    key: "Thermal decomposition temperature",
    label: "热分解温度",
    unit: "°C",
    shortLabel: "Td",
    group: "thermal"
  },
  {
    key: "Thermal decomposition weight loss",
    label: "热分解失重率",
    unit: "%",
    shortLabel: "Wloss",
    group: "thermal"
  },
  {
    key: "Elongation at break",
    label: "断裂伸长率",
    unit: "%",
    shortLabel: "ε",
    group: "mechanical"
  },
  {
    key: "Tensile stress strength at break",
    label: "断裂拉伸强度",
    unit: "MPa",
    shortLabel: "σ",
    group: "mechanical"
  },
  {
    key: "O2 Permeability Barrer",
    label: "O₂ 渗透率",
    unit: "Barrer",
    shortLabel: "P(O₂)",
    group: "permeability"
  },
  {
    key: "Co2 Permeability Barrer",
    label: "CO₂ 渗透率",
    unit: "Barrer",
    shortLabel: "P(CO₂)",
    group: "permeability"
  },
  {
    key: "H2 Permeability Barrer",
    label: "H₂ 渗透率",
    unit: "Barrer",
    shortLabel: "P(H₂)",
    group: "permeability"
  }
] as const satisfies readonly PredictPropertyDefinition[];

export const PREDICTABLE_PROPERTIES: readonly PredictableProperty[] =
  PREDICT_PROPERTY_CATALOG.map((property) => property.key);

export const DEFAULT_PREDICTABLE_PROPERTY: PredictableProperty =
  PREDICT_PROPERTY_CATALOG[0].key;

export const PREDICT_PROPERTY_META = Object.fromEntries(
  PREDICT_PROPERTY_CATALOG.map(({ key, label, unit, shortLabel, group }) => [
    key,
    { label, unit, shortLabel, group }
  ])
) as Record<
  PredictableProperty,
  Pick<PredictPropertyDefinition, "label" | "unit" | "shortLabel" | "group">
>;

export const PREDICT_PROPERTY_GROUPS: readonly {
  key: PredictPropertyGroup;
  label: string;
  description: string;
}[] = [
  { key: "thermal", label: "热学性质", description: "相变与热稳定性" },
  { key: "mechanical", label: "力学性质", description: "断裂响应与强度" },
  { key: "permeability", label: "气体渗透", description: "O₂、CO₂ 与 H₂" }
];
