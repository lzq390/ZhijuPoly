import { Activity, CheckCheck } from "lucide-react";
import { useGpuTaskHistory } from "../../hooks/useGpuTaskHistory";
import type { GpuHistoricalTask } from "../../types/gpuTaskHistory";
import "./gpu-task-history.css";

function finishedTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}
function duration(seconds: number) {
  const total = Math.round(seconds);
  if (total < 60) return `${total} 秒`;
  if (total < 3600) return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
  return `${Math.floor(total / 3600)} 小时 ${Math.floor(total % 3600 / 60)} 分`;
}
function memory(bytes: number | null) {
  if (bytes === null) return "显存 —";
  return bytes >= 2 ** 30 ? `显存 ${(bytes / 2 ** 30).toFixed(1)} GiB` : `显存 ${(bytes / 2 ** 20).toFixed(0)} MiB`;
}
const taskLabels: Record<GpuHistoricalTask["calculation_type"], string> = {
  single_point: "DFT 单点能", optimization: "DFT 结构优化", md_formal: "单体 MD · 正式", md_demo: "单体 MD · 演示", polymer_md: "聚合物 MD"
};
const environmentLabel = (environment: string) => environment === "development" ? "开发" : "生产";

export type GpuTaskHistoryProps = {
  gpuUuid: string | null;
  gpus?: Array<{ uuid: string; index: number }>;
  onSelectGpu?: (uuid: string) => void;
};

function GpuReference({ uuid, gpus, onSelectGpu }: Omit<GpuTaskHistoryProps, "gpuUuid"> & { uuid: string }) {
  const gpu = gpus?.find(item => item.uuid === uuid);
  if (!gpu || !Number.isInteger(gpu.index) || gpu.index < 0) {
    return <span className="gth-gpu-reference is-unavailable" title="此记录的 GPU 不在当前设备列表中">历史 GPU</span>;
  }
  return onSelectGpu
    ? <button type="button" className="gth-gpu-link" aria-label={`切换到 GPU ${gpu.index}`} onClick={() => onSelectGpu(uuid)}>GPU {gpu.index}</button>
    : <span className="gth-gpu-reference">GPU {gpu.index}</span>;
}

/** Current activity and retained completed records have independent source health and sampling clocks. */
export function GpuTaskHistory({ gpuUuid, gpus, onSelectGpu }: GpuTaskHistoryProps) {
  const control = useGpuTaskHistory();
  const { accessError, activityAvailable, historyAvailable, activityCached, historyCached } = control;
  const data = accessError ? null : control.data;
  const tasks = (data?.tasks ?? []).filter(task => gpuUuid === null || task.gpu_uuid === gpuUuid)
    .sort((a, b) => Date.parse(b.finished_at) - Date.parse(a.finished_at) || a.id.localeCompare(b.id)).slice(0, 2);
  const activity = (data?.activity.items ?? []).filter(item => (gpuUuid === null || item.gpu_uuid === gpuUuid) && item.kind === "task" && item.status === "running");
  const currentConfirmed = activityAvailable && !activityCached && !accessError;
  const historyConfirmed = historyAvailable && !historyCached && !accessError;
  return <div className="gth-content" aria-label="已核验 GPU 活动与历史任务">
    {accessError && <div className="gth-access"><p className="gth-access-notice" role="status">{control.error ?? "暂时无法确认任务访问权限。"}</p>{control.active && <button type="button" className="gth-access-retry" onClick={control.refresh} disabled={control.loading}>{control.loading ? "正在读取…" : "重新读取"}</button>}</div>}
    <section className="gth-group" aria-label="当前正在进行">
      <div className="gth-group-heading"><h3><Activity size={13} aria-hidden="true" />当前正在进行</h3>{activity.length > 0 && <span className="gth-count">{activity.length}</span>}</div>
      {activity.length ? <ul className="gth-active-list" aria-label="当前运行任务" tabIndex={0}>{activity.map(item => <li className="gth-active-item" key={item.id}>
        <div className="gth-task-heading"><strong>{item.label}</strong>{gpuUuid === null && <GpuReference uuid={item.gpu_uuid} gpus={gpus} onSelectGpu={onSelectGpu} />}{item.environment !== "unknown" && <span className="gth-environment">{environmentLabel(item.environment)}</span>}<span className={`gth-kind is-task${currentConfirmed ? "" : " is-cached"}`}>{currentConfirmed ? "任务运行中" : "最近运行任务"}</span></div>
        <div className="gth-task-meta"><span title={item.memory_used_bytes === null ? "显存占用暂不可用" : `${item.memory_used_bytes} 字节`}>{memory(item.memory_used_bytes)}</span>{item.started_at && <time dateTime={item.started_at} title={item.started_at}>启动 {finishedTime(item.started_at)}</time>}</div>
      </li>)}</ul> : <p className="gth-empty" role="status">{currentConfirmed ? "暂无正在进行的任务" : "--"}</p>}
    </section>
    <section className="gth-group" aria-label="近期完成任务">
      <div className="gth-group-heading"><h3><CheckCheck size={13} aria-hidden="true" />近期完成任务</h3>{tasks.length > 0 && <span className="gth-count">{tasks.length}</span>}</div>
      {tasks.length ? <ul className="gth-list" aria-label="历史任务记录">{tasks.map(task => <li className="gth-task" key={task.id}>
        <div className="gth-task-heading"><strong>{taskLabels[task.calculation_type]}</strong>{gpuUuid === null && <GpuReference uuid={task.gpu_uuid} gpus={gpus} onSelectGpu={onSelectGpu} /> }<span className="gth-environment">{environmentLabel(task.environment)}</span><span className="gth-completed">已完成</span></div>
        <div className="gth-task-meta"><time dateTime={task.finished_at} title={task.finished_at}>完成 {finishedTime(task.finished_at)}</time><span title={`${task.duration_seconds} 秒`}>耗时 {duration(task.duration_seconds)}</span></div>
      </li>)}</ul> : <p className="gth-empty" role="status">{historyConfirmed ? "暂无可关联的历史记录" : "--"}</p>}
    </section>
  </div>;
}
