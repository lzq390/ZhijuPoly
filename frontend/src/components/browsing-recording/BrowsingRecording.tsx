import {
  createContext, useCallback, useContext, useEffect, useId, useLayoutEffect,
  useRef, useState, useSyncExternalStore, type CSSProperties, type ReactNode
} from "react";
import { createPortal } from "react-dom";
import { ArrowUpRight, CircleAlert, LoaderCircle, Sparkles, X } from "lucide-react";
import { useKnowledgeRecording } from "../../hooks/useKnowledgeRecording";
import { ModalFocusSuspendedContext } from "../../hooks/useModalFocus";
import type { ActiveModule } from "../../routing";
import "../../styles/browsing-recording.css";

const DESKTOP_QUERY = "(min-width: 1024px)";
const UNSUPPORTED = "当前页面未接入，仅支持本地知识库和数据库筛选";
const OUTSIDE_RECORDING = "当前页面操作不计入记录";
const INCOMPLETE = "部分内容未能记录，总结可能不完整。";
function desktopSnapshot() {
  return typeof window.matchMedia !== "function" || window.matchMedia(DESKTOP_QUERY).matches;
}
function subscribeDesktop(onChange: () => void) {
  const media = window.matchMedia?.(DESKTOP_QUERY);
  media?.addEventListener("change", onChange);
  return () => media?.removeEventListener("change", onChange);
}

type RecordingUI = {
  activeModule: ActiveModule;
  canStart: boolean;
  desktop: boolean;
  open: boolean;
  panelId: string;
  trigger: HTMLButtonElement | null;
  setTrigger: (trigger: HTMLButtonElement | null) => void;
  setOpen: (open: boolean) => void;
  close: () => void;
};
const RecordingUIContext = createContext<RecordingUI | null>(null);

/** The session and its single panel survive route and entry-host changes. */
export function BrowsingRecordingUIProvider({ activeModule, canStart, children }: {
  activeModule: ActiveModule; canStart: boolean; children: ReactNode;
}) {
  const desktop = useSyncExternalStore(subscribeDesktop, desktopSnapshot, () => true);
  const [open, setOpen] = useState(false);
  const [trigger, setTrigger] = useState<HTMLButtonElement | null>(null);
  const panelId = useId();
  const close = useCallback(() => {
    setOpen(false);
    if (trigger?.isConnected && !trigger.closest('[inert], [hidden], [aria-hidden="true"]')) {
      const target = trigger.disabled ? trigger.parentElement : trigger;
      target?.focus({ preventScroll: true });
    }
  }, [trigger]);
  return <RecordingUIContext.Provider value={{ activeModule, canStart, desktop, open, panelId, trigger, setTrigger, setOpen, close }}>
    <ModalFocusSuspendedContext.Provider value={open}>{children}</ModalFocusSuspendedContext.Provider>
    <BrowsingRecordingPanel />
  </RecordingUIContext.Provider>;
}

/** Only the current module's desktop host OR the mobile navigation owns an entry. */
export function BrowsingRecordingControls({ module, placement = "page" }: {
  module?: ActiveModule; placement?: "page" | "toolbar" | "sidebar" | "mobile";
}) {
  const ui = useContext(RecordingUIContext);
  const recording = useKnowledgeRecording();
  const hintId = useId();
  const entryRef = useRef<HTMLSpanElement>(null);
  const [showHint, setShowHint] = useState(false);
  const active = Boolean(ui && recording && (placement === "mobile" ? !ui.desktop : ui.desktop && module === ui.activeModule));
  const startError = active && recording?.phase === "idle" ? recording.error : null;
  // Announce each failed attempt once. Keeping the error must not pin its hint open.
  useEffect(() => { setShowHint(Boolean(startError)); }, [startError, active]);
  useEffect(() => {
    if (!active || ui?.open) return;
    function onPointerDown(event: PointerEvent) {
      if (event.target instanceof Node && !entryRef.current?.contains(event.target)) setShowHint(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (!showHint || event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      event.stopPropagation();
      setShowHint(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown, true);
    };
  }, [active, showHint, ui?.open]);
  if (!ui || !recording || !active) return null;
  const { phase, pending, error, incomplete, start, stop } = recording;
  const busy = phase === "starting" || phase === "stopping" || phase === "summarizing";
  const compact = placement === "sidebar" || placement === "mobile";
  const disabled = pending > 0 || phase === "starting" || phase === "stopping" || (phase === "idle" && !ui.canStart);
  const label = phase === "recording" ? "结束并总结"
    : phase === "starting" ? "正在开始…"
    : phase === "stopping" ? "正在结束…"
    : phase === "summarizing" ? "查看生成进度"
    : phase === "stop_failed" ? "重试结束记录"
    : phase === "stopped" || phase === "summary_failed" ? "查看总结" : "开始记录";
  const notice = error || (incomplete ? INCOMPLETE : "");
  const hint = [
    `AI 浏览总结 · ${label}`,
    !ui.canStart ? phase === "idle" ? UNSUPPORTED : OUTSIDE_RECORDING : "",
    pending > 0 ? "正在等待操作完成" : phase === "recording" ? "正在记录本地知识库与数据库筛选中的检索和主动查看内容" : "",
    notice
  ].filter(Boolean).join("。 ");
  function handleClick() {
    if (disabled || !ui || !recording) return;
    setShowHint(false);
    if (phase === "idle") {
      ui.setOpen(false);
      void start();
    } else {
      ui.setOpen(true);
      if (phase === "recording" || phase === "stop_failed") void stop();
    }
  }
  return <span ref={entryRef} className={`np-recording-entry is-${placement}`} data-recording-entry={placement}
    tabIndex={disabled ? 0 : undefined} aria-label={disabled ? "AI 浏览总结" : undefined}
    aria-describedby={disabled ? hintId : undefined}
    onMouseEnter={() => setShowHint(true)} onMouseLeave={() => setShowHint(false)}
    onFocus={() => setShowHint(true)} onBlur={() => setShowHint(false)}
    onPointerDown={() => { if (disabled) setShowHint(true); }}>
    <button ref={ui.setTrigger} type="button" className={`ks-recording-trigger${phase === "recording" ? " is-recording" : ""}`}
      disabled={disabled} aria-label={compact ? `AI 浏览总结：${label}` : undefined}
      aria-expanded={ui.open} aria-controls={ui.open ? ui.panelId : undefined} aria-describedby={hintId}
      onClick={handleClick}>
      {busy ? <LoaderCircle size={16} className="ks-recording-spinner" aria-hidden="true" />
        : phase === "recording" ? <span className="ks-recording-dot" aria-hidden="true" /> : <Sparkles size={16} aria-hidden="true" />}
      {!compact ? label : null}
      {notice ? <CircleAlert size={12} className="np-recording-warning" aria-hidden="true" /> : null}
    </button>
    <span id={hintId} className={`np-recording-tooltip${showHint && !ui.open ? " is-visible" : ""}`}
      role={error && phase === "idle" ? "alert" : "tooltip"}>{hint}</span>
    <span className="sr-only" role="status">{label}{pending > 0 ? "，正在等待操作完成" : ""}{incomplete ? `，${INCOMPLETE}` : ""}</span>
  </span>;
}

function usePanelPosition(ui: RecordingUI | null) {
  const [style, setStyle] = useState<CSSProperties>({});
  useLayoutEffect(() => {
    if (!ui?.open) return;
    const main = document.querySelector<HTMLElement>(".np-app-shell__body > main");
    const header = ui.trigger?.closest<HTMLElement>(".np-module-page-header, [data-recording-header]");
    let frame = 0;
    function measure() {
      const mainRect = main?.getBoundingClientRect();
      const headerRect = header?.getBoundingClientRect();
      const margin = ui!.desktop ? 20 : 12;
      const top = ui!.desktop
        ? Math.max((mainRect?.top ?? 0) + 16, (headerRect?.bottom ?? 0) + 8)
        : (document.querySelector(".np-sidebar-mobile-header")?.getBoundingClientRect().bottom ?? 56) + 12;
      setStyle({ top, right: margin + (mainRect ? window.innerWidth - mainRect.right : 0),
        width: Math.min(620, (mainRect?.width ?? window.innerWidth) - margin * 2),
        maxHeight: Math.max(80, window.innerHeight - top - margin) });
    }
    function schedule() {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    }
    measure();
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(schedule) : null;
    if (main) observer?.observe(main);
    if (header) observer?.observe(header);
    window.addEventListener("resize", schedule);
    document.addEventListener("scroll", schedule, true);
    return () => {
      cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener("resize", schedule);
      document.removeEventListener("scroll", schedule, true);
    };
  }, [ui?.open, ui?.trigger, ui?.desktop, ui?.activeModule]);
  return style;
}

function BrowsingRecordingPanel() {
  const ui = useContext(RecordingUIContext);
  const recording = useKnowledgeRecording();
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const style = usePanelPosition(ui);
  const open = ui?.open;
  const close = ui?.close;
  const setOpen = ui?.setOpen;
  const trigger = ui?.trigger;
  const latestClose = useRef(close);
  latestClose.current = close;
  const returnFocusFrame = useRef(0);
  useEffect(() => () => cancelAnimationFrame(returnFocusFrame.current), []);
  useEffect(() => { if (open) closeRef.current?.focus({ preventScroll: true }); }, [open]);
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      event.stopPropagation();
      close?.();
    };
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !panelRef.current?.contains(event.target) && !trigger?.parentElement?.contains(event.target)) {
        setOpen?.(false);
        const target = event.target;
        // Let the clicked control receive native focus. Background text instead
        // focuses an ancestor (often main), so restore the entry after that step.
        cancelAnimationFrame(returnFocusFrame.current);
        if (target instanceof Element && target.closest('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [contenteditable], [tabindex]:not([tabindex="-1"])')) return;
        returnFocusFrame.current = requestAnimationFrame(() => {
          if (!panelRef.current && document.activeElement?.contains(target)) {
            latestClose.current?.();
          }
        });
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open, close, setOpen, trigger]);
  if (!ui || !recording || !open) return null;
  const { phase, error, incomplete, summary, partialSummary, start, stop, retrySummary } = recording;
  const busy = phase === "stopping" || phase === "summarizing";
  const content = summary?.summary || partialSummary || "";
  const paragraphs = content.split(/\n\s*\n/).filter((part) => part.trim());
  const sections = ["浏览线索", "阅读收获", "内容联系"];
  return createPortal(
    <aside ref={panelRef} id={ui.panelId} style={style} className="ks-recording-panel" role="dialog" aria-modal="false" aria-labelledby={titleId}>
      <header className="ks-recording-panel-header">
        <span className="ks-recording-emblem"><Sparkles size={21} aria-hidden="true" /></span>
        <div><span className="ks-recording-eyebrow">AI 浏览总结</span><h2 id={titleId}>本次浏览总结</h2></div>
        <button ref={closeRef} type="button" className="ks-recording-close" aria-label="关闭总结" onClick={ui.close}><X size={20} /></button>
      </header>
      <div className="ks-recording-panel-body">
        {!ui.canStart ? <p className="ks-recording-scope">{OUTSIDE_RECORDING}，仅记录本地知识库和数据库筛选。</p> : null}
        {incomplete ? <p className="ks-recording-notice" role="alert">{INCOMPLETE}</p> : null}
        {error ? <div className="ks-recording-error">
          <p role="alert">{error}</p>
          {phase === "summary_failed" ? <button type="button" className="ks-recording-trigger" onClick={() => void retrySummary()}>重试总结</button> : null}
          {phase === "stop_failed" ? <button type="button" className="ks-recording-trigger" onClick={() => void stop()}>重试结束记录</button> : null}
        </div> : null}
        {busy && !content ? <div className="ks-recording-loading">
          <div role="status"><LoaderCircle size={22} className="ks-recording-spinner" aria-hidden="true" />
            <strong>{phase === "stopping" ? "正在整理本次记录" : "正在整理本次阅读"}</strong>
            <p>你可以先收起面板，稍后从 AI 浏览总结入口查看。</p>
          </div>
          <div className="ks-recording-skeleton" aria-hidden="true"><i /><i /><i /><i /></div>
        </div> : null}
        {busy && content ? <div role="status" className="ks-recording-stream-status">
          <LoaderCircle size={16} className="ks-recording-spinner" aria-hidden="true" />正在生成总结，可边生成边阅读
        </div> : null}
        {content ? <section aria-label={summary ? summary.generated ? "AI 总结" : "记录说明" : busy ? "正在生成的总结" : "未完成的总结"}
          aria-busy={busy} className={`ks-recording-content${busy ? " is-streaming" : ""}`}>
          {paragraphs.map((paragraph, index) => <div className="ks-recording-section" key={index}>
            {summary?.generated && paragraphs.length === 3 ? <h3><span>{String(index + 1).padStart(2, "0")}</span>{sections[index]}</h3> : null}
            <p>{paragraph}</p>
          </div>)}
        </section> : null}
      </div>
      <footer className="ks-recording-panel-footer">
        <span>{busy ? "收起后继续生成" : summary?.generated ? "根据本次浏览内容生成" : "本次阅读回顾"}</span>
        <div>
          {phase === "stopped" && ui.canStart ? <button type="button" className="ks-recording-new"
            onClick={() => { ui.close(); void start(); }}>开始新记录</button> : null}
          <button type="button" className="ks-recording-dismiss" onClick={ui.close}>继续浏览<ArrowUpRight size={15} aria-hidden="true" /></button>
        </div>
      </footer>
    </aside>, document.body
  );
}
