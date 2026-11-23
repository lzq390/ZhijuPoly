import type { StructureDescriptorResponse } from "../../types";

export type StructureFactKey =
  | "formula"
  | "molecular_weight"
  | "heavy_atom_count"
  | "aromatic_ring_count"
  | "rotatable_bond_count"
  | "tpsa"
  | "attachment_point_count";

export type StructureFact = {
  key: StructureFactKey;
  label: string;
  unit?: string;
  /** One or two sentences: what the number is, and how it usually relates to polymer properties. */
  explanation: string;
};

/** The descriptors shown under the structure editor, in display order. */
export const STRUCTURE_FACTS: readonly StructureFact[] = [
  {
    key: "formula",
    label: "分子式",
    explanation: "重复单元的元素组成，不含连接点 *。"
  },
  {
    key: "molecular_weight",
    label: "分子量",
    unit: "g/mol",
    explanation: "一个重复单元的质量，不是聚合物的分子量；聚合物分子量还取决于聚合度。"
  },
  {
    key: "heavy_atom_count",
    label: "重原子",
    explanation: "除氢以外的原子数，不含连接点，反映重复单元的大小。"
  },
  {
    key: "aromatic_ring_count",
    label: "芳香环",
    explanation: "苯环等芳香环的数量。主链上的芳香环会让链更刚，通常对应更高的 Tg 和热稳定性。"
  },
  {
    key: "rotatable_bond_count",
    label: "可旋转键",
    explanation: "不在环内、可以自由旋转的单键数。数量越多链越柔顺，Tg 通常越低。"
  },
  {
    key: "tpsa",
    label: "TPSA",
    unit: "Å²",
    explanation: "拓扑极性表面积，由 N、O 等极性原子估算。数值越大，极性基团越多，链间作用通常越强，也会影响气体渗透。"
  },
  {
    key: "attachment_point_count",
    label: "连接点",
    explanation: "SMILES 中 * 的数量，表示重复单元与相邻单元相连的位置。均聚物重复单元一般有 2 个。"
  }
];

/** Homopolymer repeat units join their neighbours through exactly two connection points. */
export const EXPECTED_ATTACHMENT_POINTS = 2;

/** Ordinary SMILES (no `*`) are accepted as they are; one or more than two points are flagged, never blocked. */
export function attachmentPointWarning(count: number): string | null {
  if (count === 0 || count === EXPECTED_ATTACHMENT_POINTS) return null;
  return `有 ${count} 个连接点 *，均聚物重复单元一般为 ${EXPECTED_ATTACHMENT_POINTS} 个，请检查结构。`;
}

export function formatStructureFact(key: StructureFactKey, descriptors: StructureDescriptorResponse): string {
  switch (key) {
    case "formula":
      return descriptors.formula || "—";
    case "molecular_weight":
      return descriptors.molecular_weight.toFixed(1);
    case "tpsa":
      return descriptors.tpsa.toFixed(1);
    default:
      return String(descriptors[key]);
  }
}

/** Renders digits in a formula as subscripts, e.g. C8H8 → C₈H₈. */
export function formulaParts(formula: string): Array<{ text: string; sub: boolean }> {
  return Array.from(formula.matchAll(/(\d+)|(\D+)/g), (match) => ({
    text: match[0],
    sub: Boolean(match[1])
  }));
}
