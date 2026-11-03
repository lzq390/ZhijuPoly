import { useEffect, useRef, useState } from "react";
import { UsageRequestError } from "../../services/usage";
import type { UsageActivity } from "../../types/usage";
import { WorkbenchSelect } from "../structure-workbench/WorkbenchSelect";
import { ACTIVITY_NOTE, UsageActivityDrawer } from "./UsageActivityDrawer";
import { UsageReadFeedback } from "./UsageReadFeedback";
import { statusLabels, beijingTimestamp } from "./format";

type Props = {
  data?: UsageActivity;
  loading: boolean;
  error?: unknown;
  retry: () => void;
  active: boolean;
  offset: number;
  module: string;
  onOffsetChange: (offset: number) => void;
  onModuleChange: (module: string) => void;
};
export function UsageActivityPanel({ data, loading, error, retry, active, offset, module, onOffsetChange, onModuleChange }: Props) {
  const [selected, setSelected] = useState<{ module: string; taskId: string } | null>(null);
  const [moduleOptions, setModuleOptions] = useState<UsageActivity["modules"]>([]);
  useEffect(() => { if (data) setModuleOptions(data.modules); }, [data]);
  useEffect(() => { if (!active) setSelected(null); }, [active]);
  const trigger = useRef<HTMLElement | null>(null);
  const unavailable = error instanceof UsageRequestError && error.status === 503 && error.code === "usage_metering_disabled";
  const record = active && !unavailable && selected ? data?.items.find(item => item.module === selected.module && item.task_id === selected.taskId) ?? null : null;
  const moduleLabel = (id: string) => moduleOptions.find(item => item.id === id)?.label ?? id;
  function page(next: number) { setSelected(null); onOffsetChange(next); }
  return <>
    <section className="np-usage-section" aria-labelledby="usage-activity-heading" aria-busy={loading}>
      <div className="np-usage-section-header"><h3 id="usage-activity-heading">任务记录</h3><label className="np-usage-operation-filter" htmlFor="usage-activity-module">操作功能
        <WorkbenchSelect id="usage-activity-module" ariaLabel="操作功能" value={module} constrainToScrollParent options={[{ value: "", label: "全部功能" }, ...moduleOptions.map(item => ({ value: item.id, label: item.label }))]}
          onChange={value => { setSelected(null); onModuleChange(value); }} />
      </label></div>
      <p className="np-usage-note">{ACTIVITY_NOTE}</p>
      <p className="np-usage-note">同一任务的重试合并为一条记录。点击“刷新全部”读取最新状态。</p>
      {data?.warnings.map((warning, index) => <p key={index} className="np-usage-coverage-warning" role="note">{warning}</p>)}
      {unavailable ? <div className="np-usage-panel np-usage-empty" role="status"><h3>任务记录尚未启用</h3><p>启用任务用量记录后，可在这里查看已接入的任务。计次回执仍可单独查阅。</p><button type="button" onClick={retry}>重新检查任务记录</button></div> : <UsageReadFeedback loading={loading} error={error} hasData={Boolean(data)} label="任务记录" retry={retry} />}
      {data && !unavailable && <>
        {data.items.length ? <div className="np-usage-table-scroll" role="region" aria-label="任务记录表，可横向滚动" tabIndex={0}><table>
          <thead><tr><th>提交时间（北京时间）</th><th>功能 / 任务</th><th>最近观测状态</th><th>详情</th></tr></thead>
          <tbody>{data.items.map(item => <tr key={`${item.module}:${item.task_id}`}>
            <td><time dateTime={item.submitted_at}>{beijingTimestamp(item.submitted_at)}</time></td>
            <td>{moduleLabel(item.module)}<small className="np-usage-operation-id" title={item.task_id}>{item.task_id.length > 20 ? `${item.task_id.slice(0, 8)}…${item.task_id.slice(-4)}` : item.task_id}</small><small className="np-usage-note">{item.attempt_count.toLocaleString("zh-CN")} 次任务尝试</small></td>
            <td><span className={`np-usage-state np-usage-state-${item.status}`}>{statusLabels[item.status] ?? item.status}</span></td>
            <td><button type="button" onClick={event => { trigger.current = event.currentTarget; setSelected({ module: item.module, taskId: item.task_id }); }}>查看详情</button></td>
          </tr>)}</tbody>
        </table></div> : <p className="np-usage-note">暂无任务记录。</p>}
      </>}
      {!unavailable && (data || offset > 0) && <nav className="np-usage-pagination" aria-label="任务记录分页"><span>第 {Math.floor(offset / 25) + 1} 页</span><div>
          <button type="button" disabled={loading || offset === 0} onClick={() => page(Math.max(0, offset - 25))}>上一页任务</button>
          <button type="button" disabled={loading || !data?.has_more || offset >= 100000} onClick={() => page(offset + 25)}>下一页任务</button>
        </div></nav>}
      {offset >= 100000 && <p className="np-usage-note">已达分页查询上限，请调整功能筛选继续查找。</p>}
    </section>
    <UsageActivityDrawer record={record} moduleLabel={record ? moduleLabel(record.module) : ""} restoreFocusTarget={trigger.current} onClose={() => setSelected(null)} />
  </>;
}
