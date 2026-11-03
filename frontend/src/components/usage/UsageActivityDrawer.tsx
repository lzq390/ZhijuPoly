import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Copy, X } from "lucide-react";
import { useModalFocus } from "../../hooks/useModalFocus";
import { useMotionPresence } from "../../hooks/useMotionPresence";
import type { UsageActivityRecord } from "../../types/usage";
import { formatMetric, statusLabels, beijingTimestamp } from "./format";
import "./UsageOperationDrawer.css";

const metricLabels: Record<string, string> = {
  queue_ms: "排队时间", execution_ms: "已观测执行时间", observed_wall_ms: "任务历时",
  provider_input_tokens: "供应商输入 Tokens", provider_output_tokens: "供应商输出 Tokens",
  provider_total_tokens: "供应商总 Tokens", cached_input_tokens: "供应商缓存输入 Tokens",
  reasoning_tokens: "供应商推理 Tokens", artifact_bytes: "累计已观测产物体积"
};
export const ACTIVITY_NOTE = "独立记录不扣减次数；显示已接入任务的最近观测状态，未采集操作不会补记。结果请在原功能历史查看。";

type Props = {
  record: UsageActivityRecord | null;
  moduleLabel: string;
  taskTypeLabel: string;
  onClose: () => void;
  restoreFocusTarget?: HTMLElement | null;
};

export function UsageActivityDrawer({ record, moduleLabel, taskTypeLabel, onClose, restoreFocusTarget }: Props) {
  const open = record !== null;
  const titleId = useId();
  const anchor = useRef<HTMLSpanElement>(null);
  const [portalHost, setPortalHost] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => { setPortalHost(anchor.current?.closest<HTMLElement>(".np-usage-page") ?? document.body); }, []);
  const scope = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const presence = useMotionPresence<HTMLElement>(open, { enter: "drawerEnter", exit: "drawerExit", property: "transform" });
  useModalFocus({ active: presence.present, open, scopeRef: scope, panelRef: presence.ref, initialFocusRef: closeButton, onClose });
  const wasPresent = useRef(false);
  const returnFocus = useRef<HTMLElement | null>(null);
  useEffect(() => { if (open && restoreFocusTarget) returnFocus.current = restoreFocusTarget; }, [open, restoreFocusTarget]);
  useEffect(() => {
    let frame = 0;
    if (!presence.present && wasPresent.current) frame = requestAnimationFrame(() => {
      const target = returnFocus.current;
      if (target?.isConnected && !target.closest('[hidden], [inert], [aria-hidden="true"]')) target.focus({ preventScroll: true });
    });
    wasPresent.current = presence.present;
    return () => cancelAnimationFrame(frame);
  }, [presence.present]);
  const recordKey = record ? `${record.module}:${record.task_id}` : "closed";
  const currentKey = useRef(recordKey);
  currentKey.current = recordKey;
  const [copy, setCopy] = useState<{ key: string; message: string }>();
  async function copyId() {
    if (!record) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(record.task_id);
      if (currentKey.current === recordKey) setCopy({ key: recordKey, message: "任务 ID 已复制" });
    } catch {
      if (currentKey.current === recordKey) setCopy({ key: recordKey, message: "复制失败，请选中完整任务 ID 手动复制。" });
    }
  }

  return <><span ref={anchor} hidden />{presence.present && portalHost && createPortal(<div ref={scope} className="np-usage-drawer-layer" data-motion-active={presence.active}>
    <div className="np-usage-drawer-backdrop" aria-hidden="true" onClick={onClose} />
    <aside ref={presence.ref} {...presence.motionProps} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-hidden={!open} inert={!open} tabIndex={-1} className="np-usage-drawer">
      <header className="np-usage-drawer-header"><div><p>任务记录</p><h2 id={titleId}>任务记录详情</h2></div><button ref={closeButton} type="button" aria-label="关闭任务记录详情" onClick={onClose}><X aria-hidden="true" /></button></header>
      <div className="np-usage-drawer-content">
        <p hidden className="np-usage-note">{ACTIVITY_NOTE}</p>
        {record && <>
          <section className="np-usage-drawer-receipt" aria-label="所选任务记录">
            <div className="np-usage-section-header"><h3>{moduleLabel}</h3><span className={`np-usage-state np-usage-state-${record.status}`}>{statusLabels[record.status] ?? record.status}</span></div>
            <div className="np-usage-drawer-id"><span>任务 ID</span><code>{record.task_id}</code><button type="button" onClick={() => void copyId()}><Copy aria-hidden="true" />复制任务 ID</button></div>
            {copy?.key === recordKey && <p role="status" className="np-usage-note">{copy.message}</p>}
            <dl className="np-usage-drawer-facts">
              <div><dt>功能模块</dt><dd>{moduleLabel}</dd></div>
              <div><dt>任务类型</dt><dd>{taskTypeLabel}</dd></div>
              <div><dt>任务尝试</dt><dd>{record.attempt_count.toLocaleString("zh-CN")} 次（重试合并展示）</dd></div>
              <div><dt>最近观测状态</dt><dd>{statusLabels[record.status] ?? record.status}</dd></div>
              <div><dt>提交时间（北京时间）</dt><dd>{beijingTimestamp(record.submitted_at)}</dd></div>
              <div><dt>最近更新（北京时间）</dt><dd>{beijingTimestamp(record.updated_at)}</dd></div>
              <div><dt>最近一次尝试 ID</dt><dd><code>{record.attempt_id ?? "未记录"}</code></dd></div>
              <div><dt>开始时间（北京时间）</dt><dd>{beijingTimestamp(record.started_at)}</dd></div>
              <div><dt>结束时间（北京时间）</dt><dd>{beijingTimestamp(record.finished_at)}</dd></div>
              <div><dt>记录来源</dt><dd>{record.source || "未记录"}</dd></div>
              <div><dt>根任务 ID</dt><dd>{record.root_task_id ?? "未记录"}</dd></div>
              {record.parent_task_id && <div><dt>父任务 ID</dt><dd>{record.parent_task_id}</dd></div>}
            </dl>
          </section>
          <section className="np-usage-drawer-results" aria-label="任务观测指标"><h3>最近一次尝试的已观测指标</h3><p hidden className="np-usage-note">未采集的指标显示“未记录”，不等于零。缓存输入和推理 Tokens 属于子集，不与供应商总 Tokens 叠加。累计产物体积不代表当前存储。</p>
            {Object.keys(record.metrics).length ? <dl className="np-usage-drawer-facts">{Object.entries(record.metrics).map(([id, value]) => <div key={id}><dt>{metricLabels[id] ?? id}</dt><dd>{formatMetric(id, value)}</dd></div>)}</dl> : <p className="np-usage-note">未记录任务指标。</p>}
          </section>
        </>}
      </div>
    </aside>
  </div>, portalHost)}</>;
}
