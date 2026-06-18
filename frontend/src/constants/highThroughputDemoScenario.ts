export type HighThroughputTargetKey = "tg" | "cte" | "elongation" | "modulus";

export type HighThroughputTarget = {
  key: HighThroughputTargetKey;
  label: string;
  shortLabel: string;
  unit: string;
  target: number;
  direction: "higher" | "lower";
  color: string;
  weight: number;
  heatCenter: { x: number; y: number };
  description: string;
};

export type HighThroughputCandidate = {
  id: string;
  x: number;
  y: number;
  monomerA: string;
  monomerB: string;
  cluster: string;
  scores: Record<HighThroughputTargetKey, number>;
};

export type HighThroughputAgent = {
  id: string;
  targetKey: HighThroughputTargetKey;
  objective: string;
  currentBestId: string;
  bestValue: number;
  topCandidateIds: string[];
  recommendation: string;
  statusByStage: string[];
  progressByStage: number[];
};

export type HighThroughputBatch = {
  round: number;
  testedIds: string[];
  recommendedIds: string[];
  explanation: string;
};

export type HighThroughputStage = {
  id: string;
  label: string;
  title: string;
  body: string;
  callout: string;
  activeTargetKey: HighThroughputTargetKey;
};

export type HighThroughputFormulation = {
  components: Array<{
    id: string;
    label: string;
    sourceTargetKey: HighThroughputTargetKey;
    candidateId: string;
    description: string;
    color: string;
  }>;
  ratioPath: Array<{
    id: string;
    x: number;
    y: number;
    score: number;
    ratios: Record<string, number>;
  }>;
  finalRatio: Record<string, number>;
  achievement: Record<HighThroughputTargetKey, number>;
  finalScore: number;
  rationale: string;
};

export type HighThroughputDemoScenario = {
  materialType: string;
  materialLabel: string;
  monomerSystem: string;
  monomerAName: string;
  monomerBName: string;
  monomerACount: number;
  monomerBCount: number;
  candidateTotal: number;
  representation: string;
  budget: string;
  targets: HighThroughputTarget[];
  candidates: HighThroughputCandidate[];
  agents: HighThroughputAgent[];
  batches: HighThroughputBatch[];
  stages: HighThroughputStage[];
  formulation: HighThroughputFormulation;
};

const targetDefinitions: HighThroughputTarget[] = [
  {
    key: "tg",
    label: "玻璃化转变温度",
    shortLabel: "Tg",
    unit: "degC",
    target: 250,
    direction: "higher",
    color: "#2563eb",
    weight: 30,
    heatCenter: { x: 72, y: 31 },
    description: "寻找高耐热候选，优先靠近高 Tg 模拟热点。",
  },
  {
    key: "cte",
    label: "热膨胀系数",
    shortLabel: "CTE",
    unit: "ppm/K",
    target: 35,
    direction: "lower",
    color: "#16a34a",
    weight: 25,
    heatCenter: { x: 28, y: 36 },
    description: "寻找低热膨胀候选，目标方向是越低越好。",
  },
  {
    key: "elongation",
    label: "断裂伸长率",
    shortLabel: "Elongation",
    unit: "%",
    target: 15,
    direction: "higher",
    color: "#7c3aed",
    weight: 25,
    heatCenter: { x: 35, y: 75 },
    description: "寻找韧性更好的候选，关注延展性模拟热点。",
  },
  {
    key: "modulus",
    label: "杨氏模量",
    shortLabel: "Modulus",
    unit: "GPa",
    target: 3,
    direction: "higher",
    color: "#f97316",
    weight: 20,
    heatCenter: { x: 77, y: 71 },
    description: "寻找高刚性候选，热点与 Tg 不完全重叠。",
  },
];

const MONOMER_A_COUNT = 40;
const MONOMER_B_COUNT = 60;
const CANDIDATE_TOTAL = MONOMER_A_COUNT * MONOMER_B_COUNT;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function pseudoRandom(seed: number) {
  const value = Math.sin(seed * 12.9898) * 43758.5453;
  return value - Math.floor(value);
}

function gaussian(seedA: number, seedB: number) {
  const u1 = Math.max(pseudoRandom(seedA), 0.0001);
  const u2 = pseudoRandom(seedB);
  return Math.sqrt(-2 * Math.log(u1)) * Math.cos(Math.PI * 2 * u2);
}

function softBound(value: number, min: number, max: number) {
  const center = (min + max) / 2;
  const halfRange = (max - min) / 2;
  return center + Math.tanh((value - center) / halfRange) * halfRange;
}

function distanceScore(x: number, y: number, center: { x: number; y: number }) {
  const dx = x - center.x;
  const dy = y - center.y;
  const distance = Math.sqrt(dx * dx + dy * dy);
  return clamp(1 - distance / 70, 0, 1);
}

function buildCandidates(): HighThroughputCandidate[] {
  const kernels = [
    { cx: 17, cy: 21, sx: 5.8, sy: 3.8, rotation: -0.18, weight: 0.09, label: "rigid-aromatic" },
    { cx: 25, cy: 32, sx: 8.1, sy: 4.4, rotation: 0.08, weight: 0.1, label: "imide-rich" },
    { cx: 33, cy: 46, sx: 6.6, sy: 4.3, rotation: -0.22, weight: 0.09, label: "flexible-bridge" },
    { cx: 43, cy: 28, sx: 7.6, sy: 5.1, rotation: 0.13, weight: 0.1, label: "ether-linked" },
    { cx: 52, cy: 40, sx: 7.8, sy: 5.3, rotation: -0.06, weight: 0.11, label: "mixed-backbone" },
    { cx: 61, cy: 22, sx: 6.8, sy: 4.1, rotation: 0.21, weight: 0.09, label: "high-rigidity" },
    { cx: 70, cy: 33, sx: 8.8, sy: 5.0, rotation: -0.12, weight: 0.12, label: "fluorinated" },
    { cx: 79, cy: 20, sx: 6.0, sy: 4.2, rotation: 0.05, weight: 0.08, label: "compact-aromatic" },
    { cx: 84, cy: 44, sx: 7.3, sy: 4.5, rotation: 0.18, weight: 0.09, label: "soft-segment" },
    { cx: 66, cy: 50, sx: 6.0, sy: 3.6, rotation: -0.24, weight: 0.07, label: "elongated-chain" },
    { cx: 48, cy: 52, sx: 5.2, sy: 3.3, rotation: 0.1, weight: 0.04, label: "low-density-edge" },
    { cx: 89, cy: 33, sx: 4.5, sy: 3.0, rotation: -0.2, weight: 0.02, label: "edge-candidates" },
  ];
  const cumulativeWeights = kernels.reduce<number[]>((weights, kernel, index) => {
    weights[index] = (weights[index - 1] ?? 0) + kernel.weight;
    return weights;
  }, []);

  return Array.from({ length: CANDIDATE_TOTAL }, (_, index) => {
    const monomerAIndex = (index % MONOMER_A_COUNT) + 1;
    const monomerBIndex = Math.floor(index / MONOMER_A_COUNT) + 1;
    const chemistryHash = pseudoRandom(monomerAIndex * 131 + monomerBIndex * 29);
    const kernelIndex = cumulativeWeights.findIndex((weight) => chemistryHash <= weight);
    const kernel = kernels[kernelIndex >= 0 ? kernelIndex : kernels.length - 1];
    const neighbor = kernels[(kernelIndex + 1 + Math.floor(pseudoRandom(index + 211) * 3)) % kernels.length];
    const bridgeMix = pseudoRandom(index + 307) < 0.24 ? pseudoRandom(index + 337) * 0.42 : 0;
    const centerX = kernel.cx * (1 - bridgeMix) + neighbor.cx * bridgeMix;
    const centerY = kernel.cy * (1 - bridgeMix) + neighbor.cy * bridgeMix;
    const spreadX = kernel.sx * (1 - bridgeMix) + neighbor.sx * bridgeMix;
    const spreadY = kernel.sy * (1 - bridgeMix) + neighbor.sy * bridgeMix;
    const rotation = kernel.rotation * (1 - bridgeMix) + neighbor.rotation * bridgeMix;
    const localX = gaussian(index + 401, index + 409) * spreadX;
    const localY = gaussian(index + 419, index + 431) * spreadY;
    const structureWarpX = Math.sin(monomerAIndex * 0.57 + monomerBIndex * 0.13) * 1.2;
    const structureWarpY = Math.cos(monomerAIndex * 0.21 - monomerBIndex * 0.31) * 0.9;
    const outlier = pseudoRandom(index + 607) < 0.018;
    const rawX = centerX + localX * Math.cos(rotation) - localY * Math.sin(rotation) + structureWarpX + (outlier ? (pseudoRandom(index + 613) - 0.5) * 18 : 0);
    const rawY = centerY + localX * Math.sin(rotation) + localY * Math.cos(rotation) + structureWarpY + (outlier ? (pseudoRandom(index + 619) - 0.5) * 11 : 0);
    const x = softBound(rawX, 5, 95);
    const y = softBound(rawY, 7, 57);
    const tgScore = Math.round(180 + distanceScore(x, y, targetDefinitions[0].heatCenter) * 105 + ((index * 7) % 13));
    const cteScore = Math.round(62 - distanceScore(x, y, targetDefinitions[1].heatCenter) * 33 + ((index * 5) % 7));
    const elongationScore = Math.round(6 + distanceScore(x, y, targetDefinitions[2].heatCenter) * 18 + ((index * 3) % 5));
    const modulusScore = Math.round((1.5 + distanceScore(x, y, targetDefinitions[3].heatCenter) * 2.25 + ((index * 4) % 8) / 20) * 10) / 10;

    return {
      id: `PI-${String(index + 1).padStart(3, "0")}`,
      x,
      y,
      monomerA: `DA-${String(monomerAIndex).padStart(2, "0")}`,
      monomerB: `DM-${String(monomerBIndex).padStart(2, "0")}`,
      cluster: kernel.label,
      scores: {
        tg: tgScore,
        cte: cteScore,
        elongation: elongationScore,
        modulus: modulusScore,
      },
    };
  });
}

export const highThroughputDemoScenario: HighThroughputDemoScenario = {
  materialType: "Polyimide",
  materialLabel: "聚酰亚胺 PI 薄膜",
  monomerSystem: "二胺 x 二酐",
  monomerAName: "二胺",
  monomerBName: "二酐",
  monomerACount: MONOMER_A_COUNT,
  monomerBCount: MONOMER_B_COUNT,
  candidateTotal: CANDIDATE_TOTAL,
  representation: "PolyBERT embedding -> 2D projection",
  budget: "3 rounds x 12 samples",
  targets: targetDefinitions,
  candidates: buildCandidates(),
  agents: [
    {
      id: "agent-tg",
      targetKey: "tg",
      objective: "maximize Tg",
      currentBestId: "PI-030",
      bestValue: 276,
      topCandidateIds: ["PI-030", "PI-041", "PI-052"],
      recommendation: "右上刚性芳香簇更接近高 Tg 热点，下一轮优先验证邻近未测点。",
      statusByStage: ["待配置", "候选已生成", "定位热点", "第 2 轮迭代", "输出 p1", "进入配方池", "已解释"],
      progressByStage: [0, 10, 32, 72, 100, 100, 100],
    },
    {
      id: "agent-cte",
      targetKey: "cte",
      objective: "minimize CTE",
      currentBestId: "PI-025",
      bestValue: 29,
      topCandidateIds: ["PI-025", "PI-034", "PI-046"],
      recommendation: "左侧低 CTE 区域与高 Tg 热点存在距离，适合保留为独立组分。",
      statusByStage: ["待配置", "候选已生成", "定位低值区", "第 2 轮迭代", "输出 p2", "进入配方池", "已解释"],
      progressByStage: [0, 10, 34, 68, 100, 100, 100],
    },
    {
      id: "agent-elongation",
      targetKey: "elongation",
      objective: "maximize elongation",
      currentBestId: "PI-071",
      bestValue: 24,
      topCandidateIds: ["PI-071", "PI-060", "PI-082"],
      recommendation: "下方柔性桥连簇贡献韧性，但单独使用会牺牲模量。",
      statusByStage: ["待配置", "候选已生成", "定位韧性区", "第 3 轮迭代", "输出 p3", "进入配方池", "已解释"],
      progressByStage: [0, 10, 28, 78, 100, 100, 100],
    },
    {
      id: "agent-modulus",
      targetKey: "modulus",
      objective: "maximize modulus",
      currentBestId: "PI-085",
      bestValue: 3.7,
      topCandidateIds: ["PI-085", "PI-074", "PI-063"],
      recommendation: "右下高模量区域与高延展区域冲突，需在配方阶段折中。",
      statusByStage: ["待配置", "候选已生成", "定位刚性区", "第 2 轮迭代", "输出 p4", "进入配方池", "已解释"],
      progressByStage: [0, 10, 30, 70, 100, 100, 100],
    },
  ],
  batches: [
    {
      round: 1,
      testedIds: ["PI-008", "PI-019", "PI-030", "PI-041", "PI-052", "PI-063", "PI-074", "PI-085"],
      recommendedIds: ["PI-025", "PI-034", "PI-046", "PI-060"],
      explanation: "初始正交点覆盖二胺、二酐和主要结构簇。",
    },
    {
      round: 2,
      testedIds: ["PI-025", "PI-034", "PI-046", "PI-060", "PI-071"],
      recommendedIds: ["PI-071", "PI-082", "PI-073", "PI-054"],
      explanation: "实验回流后，各 Agent 在不同热点附近追加验证点。",
    },
    {
      round: 3,
      testedIds: ["PI-082", "PI-073", "PI-054"],
      recommendedIds: ["PI-030", "PI-025", "PI-071", "PI-085"],
      explanation: "单目标 Top-k 收敛，输出进入配方优化的候选组分。",
    },
  ],
  stages: [
    {
      id: "S0",
      label: "任务设置",
      title: "配置材料体系与多目标性能",
      body: "系统从明确的研发目标开始：选择聚酰亚胺薄膜、二胺 x 二酐单体库，并指定 Tg、CTE、断裂伸长率和杨氏模量目标。",
      callout: "重点展示系统正在优化什么，而不是直接给出材料结论。",
      activeTargetKey: "tg",
    },
    {
      id: "S1",
      label: "候选空间",
      title: "生成 a x b 候选聚合物空间",
      body: "40 个二胺与 60 个二酐形成 2,400 个理论候选。当前阶段只展示统一候选空间点云：每个点代表一个二胺 x 二酐组合，尚未叠加性能地形、已测样本或 Agent 推荐。",
      callout: "S1 的重点是确认候选全集已经生成，后续阶段才开始表征解释、抽样和优化。",
      activeTargetKey: "tg",
    },
    {
      id: "S2",
      label: "材料地图",
      title: "用 PolyBERT 表征投影为 2D 材料地图",
      body: "在同一候选点云基础上叠加 PolyBERT 材料地图解释层。热力图层表示当前性能的模拟潜力区，点位仍然代表同一批候选材料。",
      callout: "二维图是解释界面，不代表真实降维或真实预测结果。",
      activeTargetKey: "cte",
    },
    {
      id: "S3",
      label: "Agent 迭代",
      title: "单目标 Agent 并行推荐下一轮实验",
      body: "四个 Agent 分别优化一个目标性能。已测点回流后，Agent 根据模拟性能地形选择下一轮推荐区域。",
      callout: "多目标问题先拆成多个可并行说明的单目标闭环。",
      activeTargetKey: "elongation",
    },
    {
      id: "S4",
      label: "候选输出",
      title: "输出单目标最优候选 p1...pn",
      body: "每个 Agent 输出当前最优候选和 Top-k 备选。它们不是最终答案，而是下一阶段配方混合优化的组分池。",
      callout: "单目标最优不等于多目标最优，这一步为后续折中提供材料来源。",
      activeTargetKey: "modulus",
    },
    {
      id: "S5",
      label: "配方搜索",
      title: "在比例空间中演示配方混合优化",
      body: "系统把 p1...pn 作为组分，按 0.1 的比例步长构建配方空间。页面播放预设搜索路径，不运行真实模拟退火。",
      callout: "搜索轨迹用于说明折中过程，所有数值均为 mock data。",
      activeTargetKey: "tg",
    },
    {
      id: "S6",
      label: "最终解释",
      title: "输出可解释的多目标推荐配方",
      body: "最终结果展示推荐比例、综合达成率、雷达图和解释文本，说明为什么该配方在多个性能之间取得平衡。",
      callout: "演示目标是让观众理解闭环逻辑，而不是证明真实材料性能。",
      activeTargetKey: "tg",
    },
  ],
  formulation: {
    components: [
      {
        id: "p1",
        label: "p1 高 Tg",
        sourceTargetKey: "tg",
        candidateId: "PI-030",
        description: "耐热骨架组分",
        color: "#2563eb",
      },
      {
        id: "p2",
        label: "p2 低 CTE",
        sourceTargetKey: "cte",
        candidateId: "PI-025",
        description: "尺寸稳定组分",
        color: "#16a34a",
      },
      {
        id: "p3",
        label: "p3 高伸长",
        sourceTargetKey: "elongation",
        candidateId: "PI-071",
        description: "韧性补偿组分",
        color: "#7c3aed",
      },
      {
        id: "p4",
        label: "p4 高模量",
        sourceTargetKey: "modulus",
        candidateId: "PI-085",
        description: "刚性增强组分",
        color: "#f97316",
      },
    ],
    ratioPath: [
      { id: "mix-0", x: 18, y: 78, score: 66, ratios: { p1: 0.4, p2: 0.2, p3: 0.3, p4: 0.1 } },
      { id: "mix-1", x: 30, y: 65, score: 72, ratios: { p1: 0.4, p2: 0.3, p3: 0.2, p4: 0.1 } },
      { id: "mix-2", x: 43, y: 56, score: 78, ratios: { p1: 0.3, p2: 0.3, p3: 0.2, p4: 0.2 } },
      { id: "mix-3", x: 55, y: 47, score: 83, ratios: { p1: 0.3, p2: 0.2, p3: 0.2, p4: 0.3 } },
      { id: "mix-4", x: 64, y: 36, score: 88, ratios: { p1: 0.4, p2: 0.2, p3: 0.1, p4: 0.3 } },
      { id: "mix-5", x: 72, y: 28, score: 91, ratios: { p1: 0.4, p2: 0.2, p3: 0.2, p4: 0.2 } },
    ],
    finalRatio: { p1: 0.4, p2: 0.2, p3: 0.2, p4: 0.2 },
    achievement: {
      tg: 94,
      cte: 88,
      elongation: 86,
      modulus: 91,
    },
    finalScore: 90,
    rationale: "推荐配方保留高 Tg 与高模量骨架，同时加入低 CTE 和高伸长组分补偿单目标冲突，适合作为下一轮真实实验验证候选。",
  },
};
