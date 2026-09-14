import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowUpRight, LoaderCircle, Sparkles, X } from "lucide-react";
import { useKnowledgeRecording } from "../../hooks/useKnowledgeRecording";

export function KnowledgeRecordingControls({ localMode, global = false }: {
  localMode: boolean; global?: boolean;
}) {
  const recording = useKnowledgeRecording();
  const start = recording?.start;
  const phase = recording?.phase;
  // Only user actions change visibility; a delayed phase effect can override an open request.
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const panelId = useId();
  const close = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      close();
    };
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !panelRef.current?.contains(event.target) && !triggerRef.current?.contains(event.target)) {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open, close]);

  if (!recording) return null;
  const { pending, error, incomplete, summary, stop, retrySummary } = recording;
  const busy = phase === "stopping" || phase === "summarizing";
  const label = phase === "recording" ? "正在记录 · 总结"
    : phase === "starting" ? "正在开始记录…"
    : phase === "stopping" ? "正在结束记录…"
    : phase === "summarizing" ? "查看生成进度"
    : phase === "stop_failed" ? "重试结束记录"
    : phase === "stopped" || phase === "summary_failed" ? "查看总结" : "开始记录";
  const paragraphs = summary?.summary.split(/\n\s*\n/).filter((part) => part.trim()) ?? [];
  const sections = ["浏览线索", "阅读收获", "内容联系"];
  const handleClick = () => {
    if (phase === "idle") {
      if (localMode) {
        setOpen(false);
        void start?.();
      }
      return;
    }
    setOpen(true);
    if (phase === "recording" || phase === "stop_failed") void stop();
  };

  return <div className={`ks-recording-controls${global ? " is-global" : ""}`}>
    <div className="ks-recording-toolbar">
      {pending > 0 ? <span className="ks-recording-hint" role="status">正在等待操作完成…</span>
        : phase === "recording" ? <span className="ks-recording-hint" role="status">正在记录本次检索与阅读</span> : null}
      {phase === "stopped" && localMode ? <button type="button" className="ks-recording-new"
        onClick={() => { setOpen(false); void start?.(); }}>开始新记录</button> : null}
      <button ref={triggerRef} type="button" className={`ks-recording-trigger${phase === "recording" ? " is-recording" : ""}`}
        disabled={pending > 0 || phase === "starting" || phase === "stopping" || (phase === "idle" && !localMode)}
        aria-expanded={open} aria-controls={open ? panelId : undefined}
        title={phase === "idle" && !localMode ? "请在本地知识库或数据库筛选中开始记录" : undefined}
        onClick={handleClick}>
        {busy || phase === "starting" ? <LoaderCircle size={16} className="ks-recording-spinner" aria-hidden="true" />
          : phase === "recording" ? <span className="ks-recording-dot" aria-hidden="true" /> : <Sparkles size={16} aria-hidden="true" />}
        {label}
      </button>
    </div>
    {error && phase === "idle" ? <p className="ks-recording-notice" role="alert">{error}</p> : null}
    {incomplete && phase === "recording" ? <p className="ks-recording-notice" role="alert">部分内容未能记录，总结可能不完整。</p> : null}
    {open ? createPortal(
      <aside ref={panelRef} id={panelId} className="ks-recording-panel" role="dialog" aria-modal="false" aria-labelledby={titleId}>
        <header className="ks-recording-panel-header">
          <span className="ks-recording-emblem"><Sparkles size={21} aria-hidden="true" /></span>
          <div><span className="ks-recording-eyebrow">阅读回顾</span><h2 id={titleId}>本次浏览总结</h2></div>
          <button ref={closeRef} type="button" className="ks-recording-close" aria-label="关闭总结" onClick={close}><X size={20} /></button>
        </header>
        <div className="ks-recording-panel-body">
          {incomplete ? <p className="ks-recording-notice" role="alert">部分内容未能记录，总结可能不完整。</p> : null}
          {error ? <div className="ks-recording-error">
            <p role="alert">{error}</p>
            {phase === "summary_failed" ? <button type="button" className="ks-recording-trigger" onClick={() => void retrySummary()}>重试总结</button> : null}
            {phase === "stop_failed" ? <p>收起面板后，可从顶部按钮重试结束记录。</p> : null}
          </div> : null}
          {busy ? <div className="ks-recording-loading">
            <div role="status"><LoaderCircle size={22} className="ks-recording-spinner" aria-hidden="true" />
              <strong>{phase === "stopping" ? "正在整理本次记录" : "正在整理本次阅读"}</strong>
              <p>你可以先收起面板，稍后从顶部按钮查看。</p>
            </div>
            <div className="ks-recording-skeleton" aria-hidden="true"><i /><i /><i /><i /></div>
          </div> : summary ? <section aria-label={summary.generated ? "AI 总结" : "记录说明"} className="ks-recording-content">
            {paragraphs.map((paragraph, index) => <div className="ks-recording-section" key={index}>
              {summary.generated && paragraphs.length === 3 ? <h3><span>{String(index + 1).padStart(2, "0")}</span>{sections[index]}</h3> : null}
              <p>{paragraph}</p>
            </div>)}
          </section> : null}
        </div>
        <footer className="ks-recording-panel-footer">
          <span>{busy ? "收起后继续生成" : summary?.generated ? "根据本次浏览内容生成" : "本次阅读回顾"}</span>
          <button type="button" className="ks-recording-dismiss" onClick={close}>继续浏览<ArrowUpRight size={15} aria-hidden="true" /></button>
        </footer>
      </aside>, document.body
    ) : null}
  </div>;
}
