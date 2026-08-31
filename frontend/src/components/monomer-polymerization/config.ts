import type {
  MonomerPolymerizationStatusResponse,
  MonomerPolymerizationTargetClass
} from "../../types";

export type MonomerPolymerizationTargetRequirement = {
  min_monomers: number;
  max_monomers: number;
  monomer_b_required: boolean;
  note: string;
};

export const TARGET_CLASS_LABELS: Record<MonomerPolymerizationTargetClass, string> = {
  polyolefin: "Polyolefin",
  polyester: "Polyester",
  polyether: "Polyether",
  polyamide: "Polyamide",
  polyimide: "Polyimide",
  polyurethane: "Polyurethane",
  polyoxazolidone: "Polyoxazolidone",
  all: "All classes"
};

export const DEFAULT_TARGET_CLASSES: MonomerPolymerizationTargetClass[] = [
  "polyimide",
  "polyester",
  "polyamide",
  "polyurethane",
  "polyether",
  "polyolefin",
  "polyoxazolidone",
  "all"
];

export const SMIPOLY_POLYIMIDE_FIXTURE = {
  monomerA: "Nc1ccc(N)cc1",
  monomerB: "O=C1OC(=O)c2cc3c(cc21)C(=O)OC3=O"
} as const;

export const DEFAULT_TARGET_REQUIREMENTS: Record<
  MonomerPolymerizationTargetClass,
  MonomerPolymerizationTargetRequirement
> = {
  polyolefin: {
    min_monomers: 1,
    max_monomers: 2,
    monomer_b_required: false,
    note: "当前类型可只填写单体 A；填写 B 后，只显示 A 和 B 共同参与的候选。"
  },
  polyester: {
    min_monomers: 2,
    max_monomers: 2,
    monomer_b_required: true,
    note: "当前类型需要两个互补单体。"
  },
  polyether: {
    min_monomers: 1,
    max_monomers: 2,
    monomer_b_required: false,
    note: "当前类型可只填写单体 A；填写 B 后，只显示 A 和 B 共同参与的候选。"
  },
  polyamide: {
    min_monomers: 2,
    max_monomers: 2,
    monomer_b_required: true,
    note: "当前类型需要两个互补单体。"
  },
  polyimide: {
    min_monomers: 2,
    max_monomers: 2,
    monomer_b_required: true,
    note: "Polyimide 需要二胺和二酐两个互补单体。"
  },
  polyurethane: {
    min_monomers: 2,
    max_monomers: 2,
    monomer_b_required: true,
    note: "当前类型需要两个互补单体。"
  },
  polyoxazolidone: {
    min_monomers: 1,
    max_monomers: 2,
    monomer_b_required: false,
    note: "当前类型可只填写单体 A；填写 B 后，只显示 A 和 B 共同参与的候选。"
  },
  all: {
    min_monomers: 1,
    max_monomers: 2,
    monomer_b_required: false,
    note: "All classes 会在所有可用类型中查找候选，单体 B 可选。"
  }
};

const REMOTE_REQUIREMENT_TRANSLATIONS: Record<string, string> = {
  "Allows a single submitted monomer for chain-growth rules.":
    "链增长类型可只填写单体 A；填写 B 后，只显示 A 和 B 共同参与的候选。",
  "Requires two complementary monomers for the lightweight v1 workflow.":
    "当前类型需要两个互补单体。",
  "Allows a single submitted monomer when SMiPoly has a matching rule.":
    "存在匹配结果时可只填写单体 A；单体 B 可选。",
  "Requires a diamine and a dianhydride monomer.":
    "Polyimide 需要二胺和二酐两个互补单体。",
  "Allows a single submitted monomer and searches across available rule classes.":
    "All classes 会在所有可用类型中查找候选，单体 B 可选。"
};

export function getTargetRequirement(
  targetClass: MonomerPolymerizationTargetClass,
  status: MonomerPolymerizationStatusResponse | null
): MonomerPolymerizationTargetRequirement {
  const fallback = DEFAULT_TARGET_REQUIREMENTS[targetClass];
  const remote = status?.target_requirements?.[targetClass];
  if (!remote) return fallback;
  return {
    ...fallback,
    ...remote,
    note: REMOTE_REQUIREMENT_TRANSLATIONS[remote.note] ?? remote.note ?? fallback.note
  };
}
const WARNING_TRANSLATIONS: Record<string, string> = {
  "Single-monomer requests only return polymerizations that do not require another user-provided monomer.":
    "只填写一个单体时，仅显示不需要另一种单体参与的候选。",
  "Filtered SMiPoly rows that involved automatically added auxiliary molecules outside the submitted monomers.":
    "已忽略需要额外辅助分子的结果，因为这些分子不在本次输入中。",
  "SMiPoly generated no polymer candidates for the supplied monomer(s) and target class.":
    "SMiPoly 未针对当前单体和目标类型生成聚合物候选。"
};

export function localizeSmipolyWarning(warning: string) {
  const localized = WARNING_TRANSLATIONS[warning];
  return localized
    ? { text: localized, translated: true }
    : { text: warning, translated: false };
}

export function clampInteger(value: number, min: number, max: number) {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.round(value)));
}
