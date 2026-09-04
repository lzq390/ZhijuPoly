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
  conformer: "准备三维构型",
  single_point: "计算能量与原子性质",
  optimization: "几何优化",
  hessian: "计算二阶力常数",
  frequency: "计算振动频率",
  artifacts: "整理结果与文件",
  running: "计算中",
  dispatch_retry: "正在恢复计算连接",
  dispatch_failed: "计算任务启动失败",
  cancel_requested: "等待取消",
  worker_failed: "计算未能完成",
  completed: "计算完成",
  failed: "计算失败",
  cancelled: "任务已取消"
};

export function labelMonomerDftStage(stage: string): string {
  return MONOMER_DFT_STAGE_LABELS[stage as MonomerDftProgressStage] ?? "处理中";
}

const MONOMER_DFT_MESSAGE_TEXT: Record<string, string> = {
  "only one deterministic local conformer was evaluated.":
    "本次仅评估了一个自动生成的初始构型。",
  "explicit net_charge overrides smiles charge inference and differs from the encoded formal charge.":
    "已使用手动设置的总电荷，该值与结构中标注的形式电荷不同。",
  "explicit net_charge overrides smiles charge inference and matches the encoded formal charge.":
    "已使用手动设置的总电荷，该值与结构中标注的形式电荷一致。",
  "the rdkit force-field conformer did not converge.":
    "初始构型优化未完全收敛，后续计算仍使用该构型。",
  "rdkit has no mmff94 or uff parameters for this molecule; the etkdg geometry was used without force-field optimization.":
    "当前结构无法进行初始力场优化，计算使用自动生成的三维构型。"
};

const MONOMER_DFT_CODE_TEXT: Record<string, string> = {
  single_conformer: "本次仅评估了一个自动生成的初始构型。",
  net_charge_override: "已使用手动设置的总电荷，请确认该值符合当前分子体系。",
  rdkit_force_field_unavailable: "当前结构无法进行初始力场优化，计算使用自动生成的三维构型。",
  rdkit_not_converged: "初始构型优化未完全收敛，后续计算仍使用该构型。",
  submission_disabled: "计算功能暂未开放。",
  schema_not_ready: "服务正在准备，请稍后刷新。",
  worker_socket_not_configured: "计算服务暂不可用，请联系管理员。",
  worker_unavailable: "计算服务暂不可用，请稍后重试。",
  database_unavailable: "任务记录暂时无法访问，请稍后重试。",
  scientific_validation_unavailable: "暂时无法检查分子结构，请稍后重试。",
  request_normalization_mismatch: "提交参数处理失败，请刷新页面后重试。",
  invalid_idempotency_key: "任务提交信息已失效，请重新提交。",
  idempotency_conflict: "本次提交与已有请求不一致，请重新提交。",
  worker_capacity_full: "当前任务较多，请稍后重试。",
  capacity_full: "当前任务较多，请稍后重试。",
  gpu_capacity_unavailable: "当前计算资源繁忙，请稍后重试。",
  download_capacity_full: "当前下载请求较多，请稍后重试。",
  job_not_found: "未找到该任务，任务可能已过期或被删除。",
  job_not_terminal: "任务尚未完成，请稍后再试。",
  artifact_not_found: "结果文件不存在或已失效。",
  artifacts_deleted: "结果文件已删除。",
  artifact_deletion_pending: "结果文件正在删除，请稍后刷新。",
  artifact_integrity_mismatch: "结果文件校验失败，暂时无法下载。",
  artifact_bundle_invalid: "结果文件无法打包，暂时无法下载。",
  artifact_size_out_of_contract: "结果文件超出下载限制。",
  charge_out_of_range: "总电荷必须在 -5–5 范围内。",
  unsupported_isotope: "当前计算暂不支持结构中指定的同位素。",
  multi_fragment_input: "一次任务只接受一个连通分子，请移除以 . 分隔的其他组分。",
  invalid_smiles: "无法识别当前 SMILES / PSMILES，请检查结构后重试。",
  invalid_psmiles_mode: "当前连接位点处理方式与输入结构不匹配。",
  psmiles_mode_required: "检测到连接位点，请选择连接两端或补全两端。",
  invalid_psmiles: "无法按所选方式处理连接位点，请检查结构后重试。",
  invalid_scientific_request: "输入结构或计算参数不符合要求，请检查后重试。",
  unsupported_model: "所选计算方案暂不可用，请选择其他用途。",
  model_unavailable: "所选计算方案暂不可用，请选择其他用途或稍后重试。",
  unsupported_element: "所选计算方案不支持结构中的部分元素，请选择其他用途。",
  unsupported_charge: "所选计算方案不支持当前总电荷，请调整后重试。",
  unsupported_multiplicity: "所选计算方案不支持当前自旋多重度，请调整后重试。",
  invalid_electron_count: "当前总电荷会产生无效电子数，请调整后重试。",
  charge_multiplicity_mismatch: "总电荷与自旋多重度不符合当前分子的电子数，请调整后重试。",
  molecule_too_large: "当前分子超出计算规模限制，请简化结构后重试。",
  hessian_molecule_too_large: "当前分子过大，无法计算二阶力常数或振动频率。",
  conformer_generation_failed: "无法为该结构生成初始三维构型，请检查结构后重试。",
  conformer_optimization_failed: "初始三维构型优化失败，请检查结构后重试。",
  non_finite_geometry: "生成的三维构型无效，请检查结构后重试。",
  gpu_guard_blocked: "计算资源状态异常，本次任务未能启动，请稍后重试。",
  gpu_lease_lost: "计算资源连接中断，本次任务未能完成，请稍后重试。",
  gpu_runtime_unhealthy: "计算资源暂不可用，请稍后重试。",
  gpu_oom: "分子或所选计算内容超出当前计算资源容量，请简化结构或减少附加计算后重试。",
  cuda_fatal: "计算资源发生异常，本次任务未能完成，请稍后重试。",
  missing_result: "计算未返回完整结果，请重新运行任务。",
  invalid_result_shape: "计算返回的结果格式异常，请重新运行任务。",
  non_finite_result: "计算结果包含无效数值，请检查结构后重试。",
  invalid_atomic_mass: "结构中的原子质量信息无效，请检查同位素标记后重试。",
  worker_failed: "计算服务异常，本次任务未能完成，请稍后重试。",
  dispatch_failed: "计算任务未能启动，请稍后重试。",
  internal_error: "操作未能完成，请稍后重试。",
  journal_upgrade_missing_enqueue_sequence: "历史任务信息不完整，暂时无法恢复该任务。"
};

type UserFacingMonomerDftMessageOptions = {
  code?: string | null;
  fallback?: string;
};

export function userFacingMonomerDftMessage(
  message: string,
  options: UserFacingMonomerDftMessageOptions = {}
): string {
  const normalized = message.trim();
  const exactText = MONOMER_DFT_MESSAGE_TEXT[normalized.toLowerCase()];
  if (exactText) return exactText;

  if (options.code) {
    const codeText = MONOMER_DFT_CODE_TEXT[options.code];
    if (codeText) return codeText;
  }

  // Locally authored Chinese validation and action messages are already safe
  // to show. Unknown service text is deliberately replaced instead of partly
  // translating implementation terms into an unreadable mixed-language line.
  const containsImplementationTerm = /\b(?:worker|broker|schema|artifact|idempotency|cuda|rdkit|aimnet2?|unix\s+socket)\b/i
    .test(normalized);
  if (/[\u3400-\u9fff]/.test(normalized) && !containsImplementationTerm) return normalized;
  return options.fallback ?? "操作未能完成，请稍后重试。";
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
