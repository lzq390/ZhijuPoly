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
  explorationCandidateIds: string[];
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

export type HighThroughputTargetRound = HighThroughputBatch & {
  surfaceSnapshotId: "round1" | "round2" | "converged";
  currentBestId: string;
};

export type HighThroughputPropertyPoint = {
  candidateId: string;
  x: number;
  y: number;
};

export type HighThroughputSurfaceSnapshot = {
  id: "prior" | "round1" | "round2" | "converged";
  label: string;
  evidenceLabel: string;
  blobs: Array<{
    x: number;
    y: number;
    rx: number;
    ry: number;
    opacity: number;
  }>;
};

export type HighThroughputPropertySpace = {
  targetKey: HighThroughputTargetKey;
  title: string;
  priorCandidateIds: string[];
  currentBestId: string;
  candidatePoints: HighThroughputPropertyPoint[];
  surfaceSnapshots: HighThroughputSurfaceSnapshot[];
};

export type HighThroughputOrthogonalPrior = {
  label: string;
  description: string;
  candidateIds: string[];
  measurements: Record<string, Record<HighThroughputTargetKey, number>>;
};

export type HighThroughputDoeCsvRow = {
  doeRun: string;
  candidateId: string;
  monomerA: string;
  monomerB: string;
  cluster: string;
  polybertX: number;
  polybertY: number;
  propertyValue: number;
};

export type HighThroughputDoeCsvFile = {
  targetKey: HighThroughputTargetKey;
  fileName: string;
  displayName: string;
  href: string;
  propertyColumn: string;
  propertyLabel: string;
  rows: HighThroughputDoeCsvRow[];
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
  orthogonalPrior: HighThroughputOrthogonalPrior;
  propertySpaces: Record<HighThroughputTargetKey, HighThroughputPropertySpace>;
  doeCsvFiles: Record<HighThroughputTargetKey, HighThroughputDoeCsvFile>;
  roundsByTarget: Record<HighThroughputTargetKey, HighThroughputTargetRound[]>;
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

const MONOMER_A_COUNT = 120;
const MONOMER_B_COUNT = 80;
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

const ORTHOGONAL_PRIOR_IDS = [
  "PI-084", "PI-106", "PI-122", "PI-259",
  "PI-339", "PI-478", "PI-532", "PI-708",
  "PI-1016", "PI-1072", "PI-1247", "PI-1784",
];

const TARGET_INDEX: Record<HighThroughputTargetKey, number> = {
  tg: 0,
  cte: 1,
  elongation: 2,
  modulus: 3,
};

const TARGET_SPACE_CENTERS: Record<HighThroughputTargetKey, { x: number; y: number }> = {
  tg: { x: 73, y: 15 },
  cte: { x: 28, y: 19 },
  elongation: { x: 34, y: 34 },
  modulus: { x: 78, y: 35 },
};

const DOE_CSV_FILE_META: Record<HighThroughputTargetKey, {
  fileName: string;
  propertyColumn: string;
  propertyLabel: string;
}> = {
  tg: {
    fileName: "doe_prior_tg.csv",
    propertyColumn: "tg_c",
    propertyLabel: "Tg (C)",
  },
  cte: {
    fileName: "doe_prior_cte.csv",
    propertyColumn: "cte_ppm_k",
    propertyLabel: "CTE (ppm/K)",
  },
  elongation: {
    fileName: "doe_prior_elongation.csv",
    propertyColumn: "elongation_percent",
    propertyLabel: "Elongation (%)",
  },
  modulus: {
    fileName: "doe_prior_modulus.csv",
    propertyColumn: "modulus_gpa",
    propertyLabel: "Modulus (GPa)",
  },
};

function propertyPoint(
  candidate: HighThroughputCandidate,
  index: number,
  target: HighThroughputTarget,
): HighThroughputPropertyPoint {
  const targetIndex = TARGET_INDEX[target.key];
  const center = TARGET_SPACE_CENTERS[target.key];
  const normalizedScore = target.direction === "lower"
    ? clamp((target.target + 28 - candidate.scores[target.key]) / 54, 0, 1)
    : clamp((candidate.scores[target.key] - target.target + 36) / 78, 0, 1);
  const pull = 0.18 + normalizedScore * 0.34;
  const orbital = (index % 19) / 19;
  const warpX = Math.sin(index * (0.17 + targetIndex * 0.03)) * (8 + targetIndex * 1.3);
  const warpY = Math.cos(index * (0.11 + targetIndex * 0.05)) * (5 + targetIndex);
  const chemistryX = ((candidate.x + targetIndex * 17 + orbital * 23) % 92) + 4;
  const chemistryY = ((candidate.y * (0.72 + targetIndex * 0.08) + targetIndex * 9 + orbital * 11) % 40) + 4;

  return {
    candidateId: candidate.id,
    x: softBound(chemistryX * (1 - pull) + center.x * pull + warpX, 3, 97),
    y: softBound(chemistryY * (1 - pull) + center.y * pull + warpY, 3, 45),
  };
}

function uniqueIds(ids: string[]) {
  return Array.from(new Set(ids));
}

function targetScoreRange(candidates: HighThroughputCandidate[], target: HighThroughputTarget) {
  return candidates.reduce(
    (range, candidate) => ({
      min: Math.min(range.min, candidate.scores[target.key]),
      max: Math.max(range.max, candidate.scores[target.key]),
    }),
    { min: Number.POSITIVE_INFINITY, max: Number.NEGATIVE_INFINITY },
  );
}

function targetDesirability(
  candidate: HighThroughputCandidate,
  target: HighThroughputTarget,
  range: { min: number; max: number },
) {
  const span = range.max - range.min || 1;
  const normalized = (candidate.scores[target.key] - range.min) / span;
  return target.direction === "lower" ? 1 - normalized : normalized;
}

function buildSurfaceBlob(
  observations: Array<{ point: HighThroughputPropertyPoint; desirability: number; sourceWeight: number }>,
  opacityBase: number,
) {
  if (observations.length === 0) {
    return null;
  }

  const weighted = observations.reduce(
    (total, observation) => {
      const weight = observation.sourceWeight * (0.18 + Math.pow(observation.desirability, 3) * 1.82);
      return {
        x: total.x + observation.point.x * weight,
        y: total.y + observation.point.y * weight,
        weight: total.weight + weight,
      };
    },
    { x: 0, y: 0, weight: 0 },
  );
  const center = {
    x: weighted.weight > 0 ? weighted.x / weighted.weight : observations[0].point.x,
    y: weighted.weight > 0 ? weighted.y / weighted.weight : observations[0].point.y,
  };
  const variance = observations.reduce(
    (total, observation) => {
      const weight = observation.sourceWeight * (0.18 + Math.pow(observation.desirability, 2) * 1.28);
      return {
        x: total.x + Math.pow(observation.point.x - center.x, 2) * weight,
        y: total.y + Math.pow(observation.point.y - center.y, 2) * weight,
        weight: total.weight + weight,
      };
    },
    { x: 0, y: 0, weight: 0 },
  );
  const quality =
    observations.reduce((total, observation) => total + observation.desirability, 0) /
    Math.max(observations.length, 1);

  return {
    x: clamp(center.x, 4, 96),
    y: clamp(center.y, 3, 45),
    rx: clamp(Math.sqrt(variance.x / Math.max(variance.weight, 1)) * 1.8 + 7, 8, 20),
    ry: clamp(Math.sqrt(variance.y / Math.max(variance.weight, 1)) * 1.8 + 5, 5.6, 14),
    opacity: clamp(opacityBase + observations.length * 0.012 + quality * 0.14, 0.22, 0.58),
  };
}

function buildSurfaceSnapshots(
  target: HighThroughputTarget,
  candidates: HighThroughputCandidate[],
  candidatePoints: HighThroughputPropertyPoint[],
  priorCandidateIds: string[],
  rounds: HighThroughputTargetRound[],
): HighThroughputSurfaceSnapshot[] {
  const candidateById = new Map(candidates.map((candidate) => [candidate.id, candidate]));
  const pointByCandidateId = new Map(candidatePoints.map((point) => [point.candidateId, point]));
  const range = targetScoreRange(candidates, target);

  function observationsFromIds(candidateIds: string[], sourceWeight: number) {
    return uniqueIds(candidateIds)
      .map((candidateId) => {
        const candidate = candidateById.get(candidateId);
        const point = pointByCandidateId.get(candidateId);
        if (!candidate || !point) {
          return null;
        }
        return {
          candidateId,
          point,
          desirability: targetDesirability(candidate, target, range),
          sourceWeight,
        };
      })
      .filter((observation): observation is NonNullable<typeof observation> => Boolean(observation));
  }

  function makeSnapshot(
    id: HighThroughputSurfaceSnapshot["id"],
    label: string,
    evidenceLabel: string,
    measuredIds: string[],
    opacityBase: number,
  ): HighThroughputSurfaceSnapshot {
    const measuredObservations = observationsFromIds(measuredIds, 1);
    const rankedObservations = [...measuredObservations].sort(
      (a, b) => b.desirability - a.desirability,
    );
    const primaryPool = rankedObservations.slice(0, Math.min(7, rankedObservations.length));
    const primaryBlob = buildSurfaceBlob(primaryPool, opacityBase);
    const secondaryPool = primaryBlob
      ? rankedObservations
          .filter((observation) => {
            const dx = observation.point.x - primaryBlob.x;
            const dy = observation.point.y - primaryBlob.y;
            return Math.sqrt(dx * dx + dy * dy) > 13;
          })
          .slice(0, 5)
      : [];
    const secondaryBlob = buildSurfaceBlob(secondaryPool, Math.max(opacityBase - 0.1, 0.18));
    const blobs = [primaryBlob, secondaryBlob]
      .filter((blob): blob is NonNullable<typeof blob> => Boolean(blob));

    return {
      id,
      label,
      evidenceLabel,
      blobs,
    };
  }

  const round1MeasuredIds = uniqueIds([...priorCandidateIds, ...(rounds[0]?.testedIds ?? [])]);
  const round2MeasuredIds = uniqueIds([...round1MeasuredIds, ...(rounds[1]?.testedIds ?? [])]);
  const convergedMeasuredIds = uniqueIds([...round2MeasuredIds, ...(rounds[2]?.testedIds ?? [])]);

  return [
    makeSnapshot("prior", "Prior DOE Surface", "正交实验先验", priorCandidateIds, 0.2),
    makeSnapshot(
      "round1",
      "Round 1 Updated Surface",
      "先验 + 第一轮回流",
      round1MeasuredIds,
      0.27,
    ),
    makeSnapshot(
      "round2",
      "Round 2 Updated Surface",
      "先验 + 两轮回流",
      round2MeasuredIds,
      0.34,
    ),
    makeSnapshot(
      "converged",
      "Converged Single-property Surface",
      "三轮回流后的模型面",
      convergedMeasuredIds,
      0.42,
    ),
  ];
}

function buildOrthogonalPrior(candidates: HighThroughputCandidate[]): HighThroughputOrthogonalPrior {
  const candidateById = new Map(candidates.map((candidate) => [candidate.id, candidate]));
  const measurements = Object.fromEntries(
    ORTHOGONAL_PRIOR_IDS.map((candidateId) => {
      const candidate = candidateById.get(candidateId);
      const scores = candidate?.scores ?? {
        tg: 0,
        cte: 0,
        elongation: 0,
        modulus: 0,
      };
      return [candidateId, scores];
    }),
  ) as Record<string, Record<HighThroughputTargetKey, number>>;

  return {
    label: "L12 Orthogonal DOE Prior",
    description: "同一批正交实验样本先进入四个单性质空间，作为初始模型和热点图的先验观测。",
    candidateIds: ORTHOGONAL_PRIOR_IDS,
    measurements,
  };
}

function buildPropertySpaces(
  candidates: HighThroughputCandidate[],
  orthogonalPrior: HighThroughputOrthogonalPrior,
  roundsByTarget: Record<HighThroughputTargetKey, HighThroughputTargetRound[]>,
): Record<HighThroughputTargetKey, HighThroughputPropertySpace> {
  return Object.fromEntries(
    targetDefinitions.map((target) => {
      const candidatePoints = candidates.map((candidate, index) => propertyPoint(candidate, index, target));
      return [
        target.key,
        {
        targetKey: target.key,
        title: `${target.shortLabel} Space`,
        priorCandidateIds: orthogonalPrior.candidateIds,
        currentBestId: target.key === "tg"
          ? "PI-1013"
          : target.key === "cte"
            ? "PI-1219"
            : target.key === "elongation"
              ? "PI-734"
              : "PI-356",
        candidatePoints,
        surfaceSnapshots: buildSurfaceSnapshots(
          target,
          candidates,
          candidatePoints,
          orthogonalPrior.candidateIds,
          roundsByTarget[target.key],
        ),
        },
      ];
    }),
  ) as Record<HighThroughputTargetKey, HighThroughputPropertySpace>;
}

function buildDoeCsvFiles(
  candidates: HighThroughputCandidate[],
  orthogonalPrior: HighThroughputOrthogonalPrior,
  propertySpaces: Record<HighThroughputTargetKey, HighThroughputPropertySpace>,
): Record<HighThroughputTargetKey, HighThroughputDoeCsvFile> {
  const candidateById = new Map(candidates.map((candidate) => [candidate.id, candidate]));

  return Object.fromEntries(
    targetDefinitions.map((target) => {
      const meta = DOE_CSV_FILE_META[target.key];
      const pointByCandidateId = new Map(
        propertySpaces[target.key].candidatePoints.map((point) => [point.candidateId, point]),
      );
      const rows = orthogonalPrior.candidateIds
        .map((candidateId, index) => {
          const candidate = candidateById.get(candidateId);
          const point = pointByCandidateId.get(candidateId);
          if (!candidate || !point) {
            return null;
          }

          return {
            doeRun: `DOE-${String(index + 1).padStart(2, "0")}`,
            candidateId,
            monomerA: candidate.monomerA,
            monomerB: candidate.monomerB,
            cluster: candidate.cluster,
            polybertX: Number(point.x.toFixed(2)),
            polybertY: Number(point.y.toFixed(2)),
            propertyValue: candidate.scores[target.key],
          };
        })
        .filter((row): row is HighThroughputDoeCsvRow => Boolean(row));

      return [
        target.key,
        {
          targetKey: target.key,
          fileName: meta.fileName,
          displayName: `${target.shortLabel} DOE prior CSV`,
          href: `/demo-data/${meta.fileName}`,
          propertyColumn: meta.propertyColumn,
          propertyLabel: meta.propertyLabel,
          rows,
        },
      ];
    }),
  ) as Record<HighThroughputTargetKey, HighThroughputDoeCsvFile>;
}

function buildRoundsByTarget(): Record<HighThroughputTargetKey, HighThroughputTargetRound[]> {
  return {
    tg: [
      { round: 1, testedIds: ["PI-1973", "PI-699"], recommendedIds: ["PI-2326", "PI-2842"], surfaceSnapshotId: "round1", currentBestId: "PI-699", explanation: "Tg Agent 根据正交先验向高耐热区域追加验证样本。" },
      { round: 2, testedIds: ["PI-2326", "PI-2842"], recommendedIds: ["PI-1013"], surfaceSnapshotId: "round2", currentBestId: "PI-2842", explanation: "第二轮沿 Tg 高值区域边界确认热稳定窗口。" },
      { round: 3, testedIds: ["PI-1013"], recommendedIds: [], surfaceSnapshotId: "converged", currentBestId: "PI-1013", explanation: "Tg 空间完成最终验证并收敛到 p1 候选。" },
    ],
    cte: [
      { round: 1, testedIds: ["PI-1121", "PI-029"], recommendedIds: ["PI-085", "PI-575"], surfaceSnapshotId: "round1", currentBestId: "PI-1121", explanation: "CTE Agent 基于先验低值点扩展尺寸稳定区域。" },
      { round: 2, testedIds: ["PI-085", "PI-575"], recommendedIds: ["PI-1219"], surfaceSnapshotId: "round2", currentBestId: "PI-085", explanation: "第二轮验证低热膨胀边界并更新热点。" },
      { round: 3, testedIds: ["PI-1219"], recommendedIds: [], surfaceSnapshotId: "converged", currentBestId: "PI-1219", explanation: "CTE 空间完成最终验证并收敛到 p2 候选。" },
    ],
    elongation: [
      { round: 1, testedIds: ["PI-099", "PI-104"], recommendedIds: ["PI-799", "PI-1309"], surfaceSnapshotId: "round1", currentBestId: "PI-099", explanation: "Elongation Agent 从先验韧性样本向柔性桥连簇扩展。" },
      { round: 2, testedIds: ["PI-799", "PI-1309"], recommendedIds: ["PI-734"], surfaceSnapshotId: "round2", currentBestId: "PI-1309", explanation: "第二轮补充高伸长区域的外延验证。" },
      { round: 3, testedIds: ["PI-734"], recommendedIds: [], surfaceSnapshotId: "converged", currentBestId: "PI-734", explanation: "伸长率空间完成最终验证并收敛到 p3 候选。" },
    ],
    modulus: [
      { round: 1, testedIds: ["PI-734", "PI-834"], recommendedIds: ["PI-1452", "PI-1490"], surfaceSnapshotId: "round1", currentBestId: "PI-834", explanation: "Modulus Agent 在刚性增强区域进行第一轮加密。" },
      { round: 2, testedIds: ["PI-1452", "PI-1490"], recommendedIds: ["PI-356"], surfaceSnapshotId: "round2", currentBestId: "PI-1452", explanation: "第二轮验证刚性提升是否可转移到相邻结构簇。" },
      { round: 3, testedIds: ["PI-356"], recommendedIds: [], surfaceSnapshotId: "converged", currentBestId: "PI-356", explanation: "模量空间完成最终验证并收敛到 p4 候选。" },
    ],
  };
}

const demoCandidates = buildCandidates();
const demoOrthogonalPrior = buildOrthogonalPrior(demoCandidates);
const demoRoundsByTarget = buildRoundsByTarget();
const demoPropertySpaces = buildPropertySpaces(demoCandidates, demoOrthogonalPrior, demoRoundsByTarget);
const demoDoeCsvFiles = buildDoeCsvFiles(demoCandidates, demoOrthogonalPrior, demoPropertySpaces);

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
  budget: "3-round closed-loop demo",
  targets: targetDefinitions,
  candidates: demoCandidates,
  orthogonalPrior: demoOrthogonalPrior,
  propertySpaces: demoPropertySpaces,
  doeCsvFiles: demoDoeCsvFiles,
  roundsByTarget: demoRoundsByTarget,
  agents: [
    {
      id: "agent-tg",
      targetKey: "tg",
      objective: "maximize Tg",
      currentBestId: "PI-1013",
      bestValue: 296,
      topCandidateIds: ["PI-1013", "PI-2842", "PI-2326"],
      explorationCandidateIds: ["PI-1973", "PI-699", "PI-2326", "PI-2842"],
      recommendation: "Tg Agent 只读取 Tg 空间的正交先验和回流样本，持续更新耐热热点并收敛到 p1。",
      statusByStage: ["待配置", "上传先验数据", "先验建模", "单性质迭代", "输出 p1", "进入配方池", "已解释"],
      progressByStage: [0, 18, 42, 78, 100, 100, 100],
    },
    {
      id: "agent-cte",
      targetKey: "cte",
      objective: "minimize CTE",
      currentBestId: "PI-1219",
      bestValue: 29,
      topCandidateIds: ["PI-1219", "PI-1121", "PI-029"],
      explorationCandidateIds: ["PI-1121", "PI-029", "PI-085", "PI-575"],
      recommendation: "CTE Agent 只在低热膨胀空间内用 DOE 先验建立低值区域，再通过回流样本更新边界。",
      statusByStage: ["待配置", "上传先验数据", "先验建模", "单性质迭代", "输出 p2", "进入配方池", "已解释"],
      progressByStage: [0, 18, 44, 74, 100, 100, 100],
    },
    {
      id: "agent-elongation",
      targetKey: "elongation",
      objective: "maximize elongation",
      currentBestId: "PI-734",
      bestValue: 22,
      topCandidateIds: ["PI-734", "PI-1309", "PI-799"],
      explorationCandidateIds: ["PI-099", "PI-104", "PI-799", "PI-1309"],
      recommendation: "Elongation Agent 根据正交样本识别柔性桥连区域，迭代加密高伸长候选。",
      statusByStage: ["待配置", "上传先验数据", "先验建模", "单性质迭代", "输出 p3", "进入配方池", "已解释"],
      progressByStage: [0, 18, 38, 82, 100, 100, 100],
    },
    {
      id: "agent-modulus",
      targetKey: "modulus",
      objective: "maximize modulus",
      currentBestId: "PI-356",
      bestValue: 3.3,
      topCandidateIds: ["PI-356", "PI-1452", "PI-1490"],
      explorationCandidateIds: ["PI-734", "PI-834", "PI-1452", "PI-1490"],
      recommendation: "Modulus Agent 基于正交先验建立高刚性空间，回流后更新刚性热点并收敛到 p4。",
      statusByStage: ["待配置", "上传先验数据", "先验建模", "单性质迭代", "输出 p4", "进入配方池", "已解释"],
      progressByStage: [0, 18, 40, 76, 100, 100, 100],
    },
  ],
  batches: [
    {
      round: 1,
      testedIds: ["PI-1973", "PI-699", "PI-1121", "PI-029", "PI-099", "PI-104", "PI-734", "PI-834"],
      recommendedIds: ["PI-2326", "PI-2842", "PI-085", "PI-575", "PI-799", "PI-1309", "PI-1452", "PI-1490"],
      explanation: "第一轮推荐样本回流，基于 DOE 先验建立各性质 Agent 的初始判断。",
    },
    {
      round: 2,
      testedIds: ["PI-2326", "PI-2842", "PI-085", "PI-575", "PI-799", "PI-1309", "PI-1452", "PI-1490"],
      recommendedIds: ["PI-1013", "PI-1219", "PI-734", "PI-356"],
      explanation: "第二轮根据回流结果向热点和外围边界追加验证点。",
    },
    {
      round: 3,
      testedIds: ["PI-1013", "PI-1219", "PI-734", "PI-356"],
      recommendedIds: [],
      explanation: "第三轮完成验证，收敛出进入配方优化的候选组分。",
    },
  ],
  stages: [
    {
      id: "S0",
      label: "任务设置",
      title: "配置材料体系与四个单性质优化任务",
      body: "系统从材料体系和目标性质开始：选择聚酰亚胺薄膜、二胺 x 二酐单体库，并把 Tg、CTE、断裂伸长率和杨氏模量拆分为四个单性质优化空间。",
      callout: "S0 只说明优化任务和实验设计边界，不展示热点图，也不进入配方折中。",
      activeTargetKey: "tg",
    },
    {
      id: "S1",
      label: "正交先验",
      title: "生成四个单性质空间并选择正交实验样本",
      body: "120 个二胺与 80 个二酐形成 9,600 个理论候选。系统为 Tg、CTE、伸长率和模量分别生成单性质空间，并在每个空间标出同一批正交实验样本。",
      callout: "S1 只有候选点云和 DOE 样本位置，热点图尚未出现，因为还没有先验测量结果输入模型。",
      activeTargetKey: "tg",
    },
    {
      id: "S2",
      label: "先验热点",
      title: "正交实验结果回流，生成第一版单性质热点图",
      body: "正交实验样本的模拟测量值进入四个属性空间，形成每个单性质模型的第一版热点图，并据此标出 Round 1 待测推荐点。此时热点图只代表基于 DOE 先验的初始认知。",
      callout: "S2 的推荐点尚未计入已测样本；进入 S3 Round 1 后，推荐点完成实验回流，才会变成已测点并可能更新当前最优。",
      activeTargetKey: "tg",
    },
    {
      id: "S3",
      label: "单性质迭代",
      title: "四个 Agent 在各自属性空间中独立迭代",
      body: "四个 Agent 不做多目标折中，而是分别在 Tg、CTE、伸长率和模量空间内推进单性质优化。推荐样本回流后，对应空间的热点图、当前最优和目标差距同步更新。",
      callout: "S3 的重点是单性质闭环：正交先验 -> 推荐 -> 实验回流 -> 热点图更新 -> 继续推荐。",
      activeTargetKey: "tg",
    },
    {
      id: "S4",
      label: "候选输出",
      title: "输出四个单性质收敛候选 p1-p4",
      body: "每个 Agent 只输出自己属性空间的 S3 收敛候选和 Top-k 备选。页面继续按单性质切换查看，不在 S4 合并展示配方比例。",
      callout: "S4 是从单性质优化到配方搜索的交接层：p1-p4 来自 S3 末端，S5 才开始做多目标比例折中。",
      activeTargetKey: "tg",
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
        candidateId: "PI-1013",
        description: "耐热骨架组分",
        color: "#2563eb",
      },
      {
        id: "p2",
        label: "p2 低 CTE",
        sourceTargetKey: "cte",
        candidateId: "PI-1219",
        description: "尺寸稳定组分",
        color: "#16a34a",
      },
      {
        id: "p3",
        label: "p3 高伸长",
        sourceTargetKey: "elongation",
        candidateId: "PI-734",
        description: "韧性补偿组分",
        color: "#7c3aed",
      },
      {
        id: "p4",
        label: "p4 高模量",
        sourceTargetKey: "modulus",
        candidateId: "PI-356",
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
