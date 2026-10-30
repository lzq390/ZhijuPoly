import type { PolymerMdCapabilities } from "../../types/polymerMd";

export type PolymerMdServiceCheck = "checking" | "confirmed" | "unconfirmed" | "unavailable";

const REASONS: Record<string, string> = {
  feature_disabled: "正式计算尚未启用。参数可以保存，服务启用后可提交。",
  submission_disabled: "当前暂停受理新计算；已有任务与结果仍可查看。",
  schema_not_ready: "计算数据结构尚未就绪，暂时无法提交。",
  maintenance: "计算服务正在维护，暂停受理新计算；已有结果仍可查看。",
  cleanup_unconfirmed: "计算服务正在确认旧进程清理，确认完成前暂停受理。",
  recovering: "计算服务正在恢复任务状态，恢复完成前暂停受理。",
  runtime_not_ready: "科学运行环境尚未就绪，暂时无法提交。",
  scope_not_ready: "计算资源管理尚未就绪，暂时无法提交。",
  authorization_unavailable: "计算启动授权服务暂不可用，请稍后刷新。",
  monitor_unavailable: "计算进程监控暂不可用，请稍后刷新。",
  stopping: "计算服务正在停止，暂停受理新计算。",
  worker_unavailable: "计算服务连接暂不可用，请稍后刷新。",
  worker_not_ready: "计算服务尚未就绪，请稍后刷新。"
};
export function polymerMdServiceState(guest: boolean, error: string | null, capabilities: PolymerMdCapabilities | null, status: PolymerMdCapabilities | null, check?: PolymerMdServiceCheck) {
  if (guest) return { ready: false, label: "登录后提交", reason: "请登录后提交或查看本人的计算任务。" };
  if (check === "unconfirmed") return { ready: false, label: "状态待确认", reason: "本次服务状态未确认，已保留上次配置。暂停提交新计算，已有任务仍可查看；正在重新检查。" };
  if (check === "unavailable") return { ready: false, label: "连接异常", reason: error ?? "计算服务状态连续读取失败，请稍后刷新。" };
  if (check === "checking") return { ready: false, label: "检查中", reason: "正在检查计算服务…" };
  if (error) return { ready: false, label: "连接异常", reason: error };
  if (!capabilities || !status) return { ready: false, label: "检查中", reason: "正在检查计算服务…" };
  const sources = [status, capabilities];
  const blocked = sources.find(value => value.admission_reason);
  if (blocked?.admission_reason) return { ready: false, label: "暂不可提交", reason: REASONS[blocked.admission_reason] ?? "计算服务当前不可提交，请刷新查看状态。" };
  if (sources.some(value => !value.enabled)) return { ready: false, label: "暂不可提交", reason: REASONS.feature_disabled };
  if (sources.some(value => !value.schema_ready)) return { ready: false, label: "暂不可提交", reason: REASONS.schema_not_ready };
  if (sources.some(value => value.maintenance?.enabled)) return { ready: false, label: "维护中", reason: REASONS.maintenance };
  if (sources.some(value => !value.worker_ready)) return { ready: false, label: "暂不可提交", reason: REASONS.worker_not_ready };
  if (sources.some(value => !value.submit_enabled)) return { ready: false, label: "暂停受理", reason: REASONS.submission_disabled };
  return { ready: true, label: "准备就绪", reason: "计算服务已就绪。" };
}
