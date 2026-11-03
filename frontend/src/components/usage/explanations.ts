/** Static accounting explanations are temporarily hidden in the usage UI.
 * Unknown messages remain visible so actual service failures are still reported.
 */
const explanations = new Set([
  "仅展示已观测并保留的操作，不作为计次，也不改变功能权限或额度。",
  "同一任务合并展示最新尝试；历史未采集任务不会补记，OpenScience 独立服务尚未接入。",
  "操作记录不保存完整结果；请在原功能页面查看仍可用的结果。",
  "末次观测状态不保证进程已终止；无法确认的任务等待核实。",
  "统计按任务提交时间归属，不是账单；历史任务不自动补记。",
  "未观测到的指标保留为空；执行耗时不是 GPU 占用时间。",
]);

export const isUsageExplanation = (message: string) => explanations.has(message);
