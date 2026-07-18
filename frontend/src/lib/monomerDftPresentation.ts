import type {
  MonomerDftArtifact,
  MonomerDftArtifactsState,
  MonomerDftJobResponse,
  MonomerDftProgressStage,
  MonomerDftResult
} from "../types";

const MONOMER_DFT_STAGE_LABELS: Record<MonomerDftProgressStage, string> = {
  pending: "等待调度",
  queued: "任务排队",
  validating: "输入校验",
  conformer: "生成三维构象",
  single_point: "能量与原子属性推理",
  optimization: "几何优化",
  hessian: "计算 Hessian",
  frequency: "计算振动频率",
  artifacts: "整理结果与产物",
  running: "计算中",
  dispatch_retry: "等待重新连接 Worker",
  dispatch_failed: "Worker 调度失败",
  cancel_requested: "等待取消",
  worker_failed: "Worker 执行失败",
  completed: "计算完成",
  failed: "计算失败",
  cancelled: "任务已取消"
};

export function labelMonomerDftStage(stage: string): string {
  return MONOMER_DFT_STAGE_LABELS[stage as MonomerDftProgressStage] ?? "处理中";
}

type ArtifactStateSource = Pick<MonomerDftJobResponse, "artifacts_deleted"> &
  Partial<Pick<MonomerDftJobResponse, "artifacts_state">>;

type ArtifactJobState = ArtifactStateSource & Pick<MonomerDftJobResponse, "artifacts">;

export function effectiveMonomerDftArtifactsState(job: ArtifactStateSource): MonomerDftArtifactsState {
  if (job.artifacts_state) return job.artifacts_state;
  return job.artifacts_deleted ? "deleted" : "available";
}

export function isMonomerDftArtifactAvailable(
  job: ArtifactStateSource,
  artifact: Pick<MonomerDftArtifact, "available">
): boolean {
  return effectiveMonomerDftArtifactsState(job) === "available" && artifact.available;
}

export function hasAvailableMonomerDftArtifacts(job: ArtifactJobState): boolean {
  return job.artifacts.some((artifact) => isMonomerDftArtifactAvailable(job, artifact));
}

export type MonomerDftRdkitPreparationState =
  | "not_performed"
  | "converged"
  | "not_converged"
  | "unknown";

export function resolveMonomerDftRdkitPreparation(
  rdkit: MonomerDftResult["rdkit"]
): { state: MonomerDftRdkitPreparationState; inferredFromLegacyFields: boolean } {
  const hasPerformed = typeof rdkit.optimization_performed === "boolean";
  const hasState = rdkit.optimization_state != null;
  if (hasPerformed && hasState) {
    return {
      state: rdkit.optimization_state ?? "unknown",
      inferredFromLegacyFields: false
    };
  }

  // V1 results did not always record the two explicit fields.  Only infer
  // states that are unambiguous from the force-field selection and RDKit's
  // documented 0/1 minimizer status; do not equate a missing field with false.
  if (!hasPerformed && !hasState) {
    if (rdkit.force_field === "ETKDG-only") {
      return { state: "not_performed", inferredFromLegacyFields: true };
    }
    if (rdkit.force_field === "MMFF94" || rdkit.force_field === "UFF") {
      if (rdkit.optimization_status === 0) {
        return { state: "converged", inferredFromLegacyFields: true };
      }
      if (rdkit.optimization_status === 1) {
        return { state: "not_converged", inferredFromLegacyFields: true };
      }
    }
  }

  return { state: "unknown", inferredFromLegacyFields: true };
}

export function formatMonomerDftRdkitPreparation(rdkit: MonomerDftResult["rdkit"]): string {
  const preparation = resolveMonomerDftRdkitPreparation(rdkit);
  const legacySuffix = preparation.inferredFromLegacyFields ? "（旧版结果推断）" : "";
  if (preparation.state === "not_performed") {
    return `${rdkit.force_field} · 未执行 MMFF/UFF 力场优化${legacySuffix}`;
  }
  if (preparation.state === "converged") {
    return `${rdkit.force_field} · 力场优化已收敛${legacySuffix}`;
  }
  if (preparation.state === "not_converged") {
    return `${rdkit.force_field} · 力场优化未收敛${legacySuffix}`;
  }
  return `${rdkit.force_field} · 力场优化状态未知（旧版字段不足）`;
}

export function lowestMonomerDftFrequency(values: number[]): number | null {
  return values.length > 0 ? Math.min(...values) : null;
}

export function selectMonomerDftDisplayTimings(
  job: Pick<MonomerDftJobResponse, "timings">,
  _workerResult?: Pick<MonomerDftResult, "timings"> | null
): MonomerDftJobResponse["timings"] {
  // Backend job.timings includes the authoritative queue/orchestration view used by the public UI.
  return job.timings;
}
