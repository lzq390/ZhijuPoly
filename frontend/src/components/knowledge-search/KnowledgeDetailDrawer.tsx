import { PanelRightOpen, X } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode
} from "react";

export type KnowledgeDrawerTab = {
  id: string;
  label: string;
  content: ReactNode;
};

type KnowledgeDetailDrawerProps = {
  id: string;
  open: boolean;
  width: number;
  contentKey: string;
  title: string;
  subtitle?: string;
  icon?: ReactNode;
  tabs?: KnowledgeDrawerTab[];
  children?: ReactNode;
  footer?: ReactNode;
  reopenLabel?: string;
  verticalReopen?: boolean;
  showReopen?: boolean;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: () => void;
};

const MIN_DRAWER_WIDTH = 320;
const MAX_DRAWER_WIDTH = 560;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function useMobileDrawer() {
  const [mobile, setMobile] = useState(
    () =>
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 899px)").matches
  );

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(max-width: 899px)");
    const update = () => setMobile(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return mobile;
}

export function KnowledgeDetailDrawer({
  id,
  open,
  width,
  contentKey,
  title,
  subtitle,
  icon,
  tabs = [],
  children,
  footer,
  reopenLabel = "重新打开详情",
  verticalReopen = false,
  showReopen = true,
  onWidthChange,
  onClose,
  onOpen
}: KnowledgeDetailDrawerProps) {
  const drawerRef = useRef<HTMLElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);
  const [everOpened, setEverOpened] = useState(open);
  const [activeTab, setActiveTab] = useState(tabs[0]?.id ?? "");
  const [resizing, setResizing] = useState(false);
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);
  const mobile = useMobileDrawer();

  useEffect(() => {
    setActiveTab(tabs[0]?.id ?? "");
  }, [contentKey]);

  useEffect(() => {
    if (open && !wasOpenRef.current) {
      returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setEverOpened(true);
      window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    }
    if (!open && wasOpenRef.current) {
      window.requestAnimationFrame(() => returnFocusRef.current?.focus());
    }
    wasOpenRef.current = open;
  }, [open]);

  useEffect(() => {
    if (!open) return;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (!mobile || event.key !== "Tab" || !drawerRef.current) return;

      const focusable = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )
      ).filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [mobile, onClose, open]);

  useEffect(() => {
    if (!resizing) return;

    function handlePointerMove(event: PointerEvent) {
      if (!dragState.current) return;
      onWidthChange(
        clamp(
          dragState.current.startWidth + dragState.current.startX - event.clientX,
          MIN_DRAWER_WIDTH,
          MAX_DRAWER_WIDTH
        )
      );
    }

    function stopResize() {
      dragState.current = null;
      setResizing(false);
    }

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopResize);
    window.addEventListener("pointercancel", stopResize);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResize);
      window.removeEventListener("pointercancel", stopResize);
    };
  }, [onWidthChange, resizing]);

  function startResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (mobile) return;
    event.preventDefault();
    dragState.current = { startX: event.clientX, startWidth: width };
    setResizing(true);
  }

  function resizeWithKeyboard(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const step = event.shiftKey ? 40 : 10;
    onWidthChange(
      clamp(width + (event.key === "ArrowLeft" ? step : -step), MIN_DRAWER_WIDTH, MAX_DRAWER_WIDTH)
    );
  }

  function changeTabWithKeyboard(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || tabs.length < 2) return;
    event.preventDefault();
    let nextIndex = index;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else nextIndex = (index + (event.key === "ArrowLeft" ? -1 : 1) + tabs.length) % tabs.length;
    const nextTab = tabs[nextIndex];
    setActiveTab(nextTab.id);
    window.requestAnimationFrame(() => {
      drawerRef.current?.querySelector<HTMLButtonElement>(`[data-ks-drawer-tab="${nextTab.id}"]`)?.focus();
    });
  }

  const selectedTab = tabs.find((tab) => tab.id === activeTab) ?? tabs[0];
  const titleId = `${id}-title`;

  return (
    <>
      <button
        className={`ks-drawer-backdrop${open && mobile ? " is-open" : ""}`}
        type="button"
        tabIndex={open && mobile ? 0 : -1}
        aria-label="关闭详情抽屉"
        onClick={onClose}
      />

      {everOpened && !open && showReopen ? (
        <button
          className={`ks-drawer-reopen${verticalReopen ? " is-vertical" : ""}`}
          type="button"
          onClick={onOpen}
          aria-expanded="false"
          aria-controls={id}
        >
          <PanelRightOpen aria-hidden="true" />
          <span>{reopenLabel}</span>
        </button>
      ) : null}

      <aside
        ref={drawerRef}
        id={id}
        className={`ks-detail-drawer${open ? " is-open" : ""}${resizing ? " is-resizing" : ""}`}
        style={{ "--ks-drawer-width": `${width}px` } as CSSProperties}
        role="dialog"
        aria-modal={mobile ? "true" : undefined}
        aria-labelledby={titleId}
        aria-hidden={!open}
        inert={!open}
      >
        <div
          className="ks-drawer-resizer"
          role="separator"
          tabIndex={open && !mobile ? 0 : -1}
          aria-label="调整详情抽屉宽度"
          aria-orientation="vertical"
          aria-valuemin={MIN_DRAWER_WIDTH}
          aria-valuemax={MAX_DRAWER_WIDTH}
          aria-valuenow={Math.round(width)}
          onPointerDown={startResize}
          onKeyDown={resizeWithKeyboard}
        />

        <header className="ks-drawer-header">
          <div className="ks-drawer-heading">
            <span className="ks-drawer-mark">{icon ?? <PanelRightOpen aria-hidden="true" />}</span>
            <div>
              <h2 id={titleId}>{title}</h2>
              {subtitle ? <p>{subtitle}</p> : null}
            </div>
          </div>
          <button ref={closeButtonRef} className="ks-icon-button" type="button" onClick={onClose} aria-label="关闭详情">
            <X aria-hidden="true" />
          </button>
        </header>

        {tabs.length > 1 ? (
          <div className="ks-drawer-tabs" role="tablist" aria-label="详情内容">
            {tabs.map((tab, index) => (
              <button
                key={tab.id}
                data-ks-drawer-tab={tab.id}
                type="button"
                role="tab"
                aria-selected={selectedTab?.id === tab.id}
                tabIndex={selectedTab?.id === tab.id ? 0 : -1}
                onClick={() => setActiveTab(tab.id)}
                onKeyDown={(event) => changeTabWithKeyboard(event, index)}
              >
                {tab.label}
              </button>
            ))}
          </div>
        ) : null}

        <div className="ks-drawer-body">{selectedTab?.content ?? children}</div>
        {footer ? <footer className="ks-drawer-footer">{footer}</footer> : null}
      </aside>
    </>
  );
}
