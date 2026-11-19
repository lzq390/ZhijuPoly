import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";
import "./module-frame.css";

export type ModuleDockTab = {
  id: string;
  label: string;
  content: ReactNode;
  /** Content manages its own scrolling and padding, e.g. a chat with a pinned composer. */
  fill?: boolean;
};

type ModuleDockProps = {
  id: string;
  open: boolean;
  tabs: readonly ModuleDockTab[];
  activeTab: string;
  onTabChange: (id: string) => void;
  onClose: () => void;
  /** Focus returns here when the dock closes while focus is inside it. */
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** Content that renders its own header (title + close) instead of the dock's. */
  bare?: { labelledBy: string };
  /** Element to focus when the dock opens; defaults to the active tab or title. */
  initialFocusSelector?: string;
  className?: string;
};

/**
 * Auxiliary side dock (AI assistant, recording summary, usage help, …).
 * One dock per module and one tab at a time. It stays mounted while closed so
 * drafts and scroll positions survive. Wide screens place it beside the work
 * surface; narrower screens float it over the surface's right edge; phones
 * show it as a full-height sheet.
 */
export function ModuleDock({
  id,
  open,
  tabs,
  activeTab,
  onTabChange,
  onClose,
  returnFocusRef,
  bare,
  initialFocusSelector,
  className
}: ModuleDockProps) {
  const rootRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const wasOpen = useRef(open);
  const active = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];

  useEffect(() => {
    const root = rootRef.current;
    if (open && !wasOpen.current) {
      const frame = window.requestAnimationFrame(() => {
        const target = (initialFocusSelector && root?.querySelector<HTMLElement>(initialFocusSelector)) ||
          root?.querySelector<HTMLElement>("[data-dock-initial-focus]");
        target?.focus({ preventScroll: true });
      });
      wasOpen.current = true;
      return () => window.cancelAnimationFrame(frame);
    }
    if (!open && wasOpen.current) {
      wasOpen.current = false;
      const focusLeftDock = !document.activeElement || document.activeElement === document.body ||
        Boolean(root?.contains(document.activeElement));
      const target = returnFocusRef?.current;
      if (focusLeftDock && target?.isConnected) target.focus({ preventScroll: true });
    }
  }, [open, initialFocusSelector, returnFocusRef]);

  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  // Escape closes the dock when focus is inside it, or nowhere in particular (e.g. after an
  // action button inside it disappeared). Focus in other page controls keeps their own Escape.
  useEffect(() => {
    if (!open) return;
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const active = document.activeElement;
      if (active && active !== document.body && !rootRef.current?.contains(active)) return;
      event.preventDefault();
      closeRef.current();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open]);

  return (
    <aside
      ref={rootRef}
      id={id}
      className={["np-module-dock", bare ? "is-bare" : "", className].filter(Boolean).join(" ")}
      aria-labelledby={bare ? bare.labelledBy : titleId}
      hidden={!open}
    >
      {bare ? null : (
        <header className="np-module-dock__header">
          {tabs.length > 1 ? (
            <div className="np-module-dock__tabs" role="tablist" aria-labelledby={titleId}>
              <span id={titleId} className="np-module-dock__sr">辅助面板</span>
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  role="tab"
                  aria-selected={tab.id === active.id}
                  className={tab.id === active.id ? "is-active" : undefined}
                  data-dock-initial-focus={tab.id === active.id ? "" : undefined}
                  onClick={() => onTabChange(tab.id)}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          ) : (
            <h2 id={titleId} tabIndex={-1} data-dock-initial-focus="">{active.label}</h2>
          )}
          <button type="button" className="np-module-dock__close" aria-label={`关闭${active.label}`} onClick={onClose}>
            <X aria-hidden="true" />
          </button>
        </header>
      )}
      <div
        className={`np-module-dock__body${active.fill ? " is-fill" : ""}`}
        role={!bare && tabs.length > 1 ? "tabpanel" : undefined}
        aria-label={!bare && tabs.length > 1 ? active.label : undefined}
      >
        {active.content}
      </div>
    </aside>
  );
}
