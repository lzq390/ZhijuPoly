import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Copy, X } from "lucide-react";
import { useModalFocus } from "../../hooks/useModalFocus";
import { useMotionPresence } from "../../hooks/useMotionPresence";
import { useUsageRead } from "../../hooks/useUsageRead";
import { fetchUsageOperation, fetchUsageOperationResult } from "../../services/entitlements";
import type { OperationState, UsageOperationResult } from "../../types/entitlements";
import type { UsageModuleGroup } from "../../types/usage";
import { beijingTimestamp } from "./format";
import { usageModuleGroupLabel, usageTaskTypeLabel } from "./moduleGroups";
import "./UsageOperationDrawer.css";

export const operationStateLabels: Record<OperationState, string> = {
  reserved: "已预留", dispatching: "派发中", running: "处理中", unknown: "结果待确认",
  succeeded: "已计成功次数", released: "已释放预留"
};
const failure = (error: unknown) => error instanceof Error ? error.message : "读取失败，请重试读取。";

type Props = {
  operationId: string | null;
  requestKey: number;
  readResultOnOpen?: boolean;
  moduleLabels: Record<string, string>;
  moduleGroups: UsageModuleGroup[];
  onClose: () => void;
  restoreFocusTarget?: HTMLElement | null;
};

export function UsageOperationDrawer({ operationId, requestKey, readResultOnOpen = false, moduleLabels, moduleGroups, onClose, restoreFocusTarget }: Props) {
  const open = operationId !== null;
  const titleId = useId();
  const anchor = useRef<HTMLSpanElement>(null);
  const [portalHost, setPortalHost] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => {
    // Stay inside the page's theme and visibility boundary, but outside the
    // scrolling/container-query content that would contain fixed positioning.
    setPortalHost(anchor.current?.closest<HTMLElement>(".np-usage-page") ?? document.body);
  }, []);
  const scope = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const presence = useMotionPresence<HTMLElement>(open, { enter: "drawerEnter", exit: "drawerExit", property: "transform" });
  useModalFocus({ active: presence.present, open, scopeRef: scope, panelRef: presence.ref, initialFocusRef: closeButton, onClose });
  const wasPresent = useRef(false);
  const returnFocus = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (open && restoreFocusTarget) returnFocus.current = restoreFocusTarget;
  }, [open, restoreFocusTarget]);
  useEffect(() => {
    let frame = 0;
    if (!presence.present && wasPresent.current) {
      frame = window.requestAnimationFrame(() => {
        const target = returnFocus.current;
        if (target?.isConnected && !target.closest('[hidden], [inert], [aria-hidden="true"]')) target.focus({ preventScroll: true });
      });
    }
    wasPresent.current = presence.present;
    return () => window.cancelAnimationFrame(frame);
  }, [presence.present]);

  const scopeKey = `${operationId ?? "closed"}:${requestKey}`;
  const receipt = useUsageRead(`operation:${scopeKey}`, signal => fetchUsageOperation(operationId!, signal), 0, open);
  const resultController = useRef<AbortController | null>(null);
  const initialResultRead = useRef<string | null>(null);
  const activeScope = useRef(scopeKey);
  activeScope.current = scopeKey;
  const [resultState, setResultState] = useState<{ key: string; loading: boolean; data?: UsageOperationResult; error?: unknown }>();
  const [copyState, setCopyState] = useState<{ key: string; message: string }>();
  useEffect(() => () => { resultController.current?.abort(); }, [scopeKey]);

  const readResult = useCallback(async () => {
    if (!operationId) return;
    resultController.current?.abort();
    const controller = new AbortController();
    resultController.current = controller;
    setResultState({ key: scopeKey, loading: true });
    try {
      const data = await fetchUsageOperationResult(operationId, controller.signal);
      if (!controller.signal.aborted && activeScope.current === scopeKey) setResultState({ key: scopeKey, loading: false, data });
    } catch (error) {
      if (!controller.signal.aborted && activeScope.current === scopeKey) setResultState({ key: scopeKey, loading: false, error });
    }
  }, [operationId, scopeKey]);

  // This request is authorized only by the explicit list action. Opening a
  // receipt, refreshing data, or an unknown result never starts another read.
  useEffect(() => {
    if (open && readResultOnOpen && receipt.data && initialResultRead.current !== scopeKey) {
      initialResultRead.current = scopeKey;
      void readResult();
    }
  }, [open, readResultOnOpen, receipt.data, scopeKey, readResult]);

  const result = resultState?.key === scopeKey ? resultState : undefined;
  const operation = result?.data?.operation ?? receipt.data;
  async function copyId() {
    if (!operationId) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(operationId);
      if (activeScope.current === scopeKey) setCopyState({ key: scopeKey, message: "操作 ID 已复制" });
    } catch {
      if (activeScope.current === scopeKey) setCopyState({ key: scopeKey, message: "复制失败，请选中完整操作 ID 手动复制。" });
    }
  }

  return <><span ref={anchor} hidden />{presence.present && portalHost && createPortal(<div ref={scope} className="np-usage-drawer-layer" data-motion-active={presence.active}>
    <div className="np-usage-drawer-backdrop" aria-hidden="true" onClick={onClose} />
    <aside ref={presence.ref} {...presence.motionProps} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-hidden={!open} inert={!open} tabIndex={-1} className="np-usage-drawer">
      <header className="np-usage-drawer-header"><div><p>操作回执</p><h2 id={titleId}>操作详情</h2></div><button ref={closeButton} type="button" aria-label="关闭操作详情" onClick={onClose}><X aria-hidden="true" /></button></header>
      <div className="np-usage-drawer-content">
        <p hidden className="np-usage-note">这里只读取已有回执和结果，不提交、重试或重算任务。</p>
        {receipt.loading && <p role="status">正在读取已有回执…</p>}
        {receipt.error != null && <div className="np-usage-error"><p role="alert">{failure(receipt.error)}</p><button type="button" onClick={receipt.retry}>重试读取回执</button></div>}
        {operation && <>
          <section aria-label="所选操作回执" className="np-usage-drawer-receipt">
            <div className="np-usage-section-header"><h3>{usageModuleGroupLabel(operation.module, moduleGroups)}</h3><span className={`np-usage-state np-usage-operation-state-${operation.state}`}>{operationStateLabels[operation.state]}</span></div>
            <div className="np-usage-drawer-id"><span>操作 ID</span><code>{operation.operation_id}</code><button type="button" onClick={() => void copyId()}><Copy aria-hidden="true" />复制操作 ID</button></div>
            {copyState?.key === scopeKey && <p role="status" className="np-usage-note">{copyState.message}</p>}
            <dl className="np-usage-drawer-facts">
              <div><dt>功能模块</dt><dd>{usageModuleGroupLabel(operation.module, moduleGroups)}</dd></div>
              <div><dt>任务类型</dt><dd>{usageTaskTypeLabel(operation.module, moduleLabels)}</dd></div>
              <div><dt>创建时间（北京时间）</dt><dd>{beijingTimestamp(operation.created_at)}</dd></div>
              <div><dt>更新时间（北京时间）</dt><dd>{beijingTimestamp(operation.updated_at)}</dd></div>
              <div><dt>策略版本</dt><dd>{operation.policy_version}</dd></div>
              <div><dt>结果判定</dt><dd>{operation.outcome ?? "待确认"}</dd></div>
              <div><dt>归属周期（北京时间）</dt><dd>{beijingTimestamp(operation.period_start)} 至 {beijingTimestamp(operation.period_end)}（结束时刻不包含在内）</dd></div>
              {operation.native_task_id && <div><dt>原生任务</dt><dd>{operation.native_task_id}</dd></div>}
            </dl>
            <details className="np-usage-disclosure"><summary tabIndex={0}>已确认的提交规格</summary><pre>{JSON.stringify(operation.spec, null, 2)}</pre></details>
          </section>
          <section className="np-usage-drawer-results" aria-label="操作结果读取">
            <h3>已有结果</h3><p hidden className="np-usage-note">结果待确认时，预留仍保留。确认结束后，在原功能中主动再次提交会创建新操作。</p>
            <button type="button" className="np-usage-primary" disabled={result?.loading} onClick={() => void readResult()}>读取已有结果</button>
            {result?.loading && <p role="status">正在读取已有结果…</p>}
            {result?.error != null && <p role="alert" className="np-usage-error">{failure(result.error)}</p>}
            {result?.data && <div aria-label="已恢复操作结果">
              <p role="status">已有结果已读取，未重新执行任务。</p>
              {result.data.result != null && <details open className="np-usage-disclosure"><summary tabIndex={0}>已有结果内容</summary><pre>{JSON.stringify(result.data.result, null, 2)}</pre></details>}
              {result.data.result_ref != null && <details className="np-usage-disclosure"><summary tabIndex={0}>原生结果引用</summary><pre>{JSON.stringify(result.data.result_ref, null, 2)}</pre><p hidden className="np-usage-note">此处展示原引用，不自动访问外部地址或重新计算。可在对应功能的任务历史中查阅产物。</p></details>}
            </div>}
          </section>
        </>}
      </div>
    </aside>
  </div>, portalHost)}</>;
}
