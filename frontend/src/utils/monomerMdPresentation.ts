import type { MonomerMdJobResponse, MonomerMdServiceStatusResponse, MonomerMdSimulationResult } from "../types";

const GENERIC_DEMO_WARNINGS = new Set([
  "Density demo output is not equilibrated and is not a physical density estimate.",
  "300-step demo output is not equilibrated and is not a physical density estimate.",
  "1000-step demo output is not equilibrated and is not a physical density estimate."
]);

function positiveInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

export function monomerMdDemoStepCount(
  result: MonomerMdSimulationResult,
  job: Pick<MonomerMdJobResponse, "completed_steps" | "requested_steps"> | null
): number | null {
  const explicit = positiveInteger(job?.completed_steps) ?? positiveInteger(job?.requested_steps);
  if (explicit != null) {
    return explicit;
  }
  const summarySteps = positiveInteger(result.summary?.n_steps);
  if (summarySteps != null) {
    return summarySteps;
  }
  for (const warning of result.warnings ?? []) {
    const match = /^(\d+)-step demo output/.exec(warning.trim());
    if (match) {
      return Number(match[1]);
    }
  }
  return null;
}

export function monomerMdDemoNotice(
  result: MonomerMdSimulationResult,
  job: Pick<MonomerMdJobResponse, "completed_steps" | "requested_steps"> | null
): string | null {
  const hasDemoWarning = (result.warnings ?? []).some((warning) => GENERIC_DEMO_WARNINGS.has(warning.trim()));
  if (!result.not_equilibrated && result.physical_density_estimate !== false && !hasDemoWarning) {
    return null;
  }
  const steps = monomerMdDemoStepCount(result, job);
  return steps == null
    ? "演示结果尚未达到平衡，不能作为物理密度估计。"
    : `${steps} 步演示结果尚未达到平衡，不能作为物理密度估计。`;
}

export function isGenericMonomerMdDemoWarning(warning: string): boolean {
  return GENERIC_DEMO_WARNINGS.has(warning.trim());
}

export function monomerMdServiceCanSubmit(
  serviceStatus: MonomerMdServiceStatusResponse | null,
  isStatusLoading: boolean,
  statusError: string | null
): boolean {
  return !isStatusLoading && statusError == null && serviceStatus?.can_submit === true;
}
