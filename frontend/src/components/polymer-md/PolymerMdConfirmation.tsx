import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

export type PolymerMdConfirmationAction = "cancel" | "clear" | "delete";
const COPY = {
  cancel: { title: "确认取消任务", action: "确认取消", description: "取消后将停止后续计算，并等待当前计算进程全部退出。已经完成的阶段和可用结果会保留。" },
  clear: { title: "确认清理输出文件", action: "确认清理", description: "清理后将无法下载这些文件，也无法浏览对应轨迹。任务记录和已汇总物性会保留，输出文件不能恢复。" },
  delete: { title: "确认删除任务记录", action: "确认删除", description: "将删除此任务及其输出文件，删除后无法恢复。请先保存需要的结果和产物。" }
} as const;

export function PolymerMdConfirmation({ action, jobId, onDismiss, onConfirm }: {
  action: PolymerMdConfirmationAction; jobId: string; onDismiss: () => void; onConfirm: () => void;
}) {
  const title = useId(), description = useId();
  const panel = useRef<HTMLDivElement>(null);
  const dismiss = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dismiss.current?.focus();
    function keyboard(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); onDismiss(); }
      if (event.key !== "Tab") return;
      const buttons = panel.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
      if (!buttons?.length) return;
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", keyboard);
    return () => { document.removeEventListener("keydown", keyboard); if (previous?.isConnected) previous.focus(); };
  }, [onDismiss]);
  const copy = COPY[action];
  return createPortal(<div className="np-polymer-md-dialog-layer np-workbench-theme" onClick={event => { if (event.target === event.currentTarget) onDismiss(); }}>
    <div ref={panel} className="np-polymer-md-dialog" role="alertdialog" aria-modal="true" aria-labelledby={title} aria-describedby={description}>
      <h2 id={title}>{copy.title}</h2><p id={description}>{copy.description}</p><p>任务：<code className="np-polymer-md-id">{jobId}</code></p>
      <div className="np-polymer-md-actions"><button ref={dismiss} type="button" onClick={onDismiss}>返回</button><button type="button" className="is-danger" onClick={onConfirm}>{copy.action}</button></div>
    </div>
  </div>, document.body);
}
