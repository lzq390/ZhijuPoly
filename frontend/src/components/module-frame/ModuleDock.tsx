import { X } from "lucide-react";
import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode, type RefObject } from "react";
import "./module-frame.css";

export type ModuleDockTab = {
  id: string;
  label: string;
  content: ReactNode;
};

type ModuleDockProps = {
  id: string;
  tabs: readonly ModuleDockTab[];
  activeTab: string;
  onTabChange: (id: string) => void;
  onClose: () => void;
  /** Focus returns here when the dock closes. */
  returnFocusRef?: RefObject<HTMLElement | null>;
};

/**
 * Auxiliary side dock (AI assistant, recording summary, usage help, …).
 * One dock per module and one tab at a time. Wide screens place it beside
 * the work surface; narrower screens float it over the surface's right edge;
 * phones show it as a full-height sheet.
 */
export function ModuleDock({ id, tabs, activeTab, onTabChange, onClose, returnFocusRef }: ModuleDockProps) {
  const rootRef = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const active = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];

  useEffect(() => {
    rootRef.current?.querySelector<HTMLElement>("[data-dock-initial-focus]")?.focus({ preventScroll: true });
    const returnTarget = returnFocusRef?.current;
    return () => {
      if (returnTarget?.isConnected) returnTarget.focus({ preventScroll: true });
    };
  }, [returnFocusRef]);

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    event.preventDefault();
    onClose();
  }

  return (
    <aside ref={rootRef} id={id} className="np-module-dock" aria-labelledby={titleId} onKeyDown={handleKeyDown}>
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
      <div className="np-module-dock__body" role={tabs.length > 1 ? "tabpanel" : undefined}>
        {active.content}
      </div>
    </aside>
  );
}
