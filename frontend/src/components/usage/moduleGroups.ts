import type { UsageModuleGroup } from "../../types/usage";

type ModuleLabels = ReadonlyArray<{ id: string; label: string }> | Record<string, string>;
export const USAGE_UNASSIGNED_NOTE = "部分历史或公共记录没有独立模块归属，可在“全部模块”查看。";

/** Display metadata only: filtering and pagination remain server operations. */
export function usageModuleGroupLabel(moduleId: string, groups: readonly UsageModuleGroup[] = []) {
  return groups.find(group => group.modules.includes(moduleId))?.label ?? "未归属侧栏模块";
}

export function usageTaskTypeLabel(moduleId: string, modules: ModuleLabels = []) {
  if (moduleId === "database_browser") return "历史数据库操作（未细分）";
  if (moduleId === "monomer_md") return "单体分子动力学";
  if (moduleId === "polymer_md") return "聚合物分子动力学";
  const label = Array.isArray(modules)
    ? modules.find(module => module.id === moduleId)?.label
    : (modules as Record<string, string>)[moduleId];
  return label ?? (moduleId === "openscience" ? "OpenScience 独立服务" : moduleId);
}

/** Full IDs remain available in the title, details and clipboard. */
export function shortUsageTaskId(taskId: string, moduleId: string) {
  const prefix = `${moduleId}:`;
  const id = taskId.startsWith(prefix) ? taskId.slice(prefix.length) : taskId;
  return id.length > 20 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}
