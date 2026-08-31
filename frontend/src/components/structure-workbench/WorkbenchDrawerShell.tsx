import { X } from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode
} from "react";

const DEFAULT_MIN_WIDTH = 320;
const DEFAULT_MAX_WIDTH = 560;
const DEFAULT_KEYBOARD_STEP = 16;
const OVERLAY_CONTAINER_WIDTH = 1280;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function focusableElements(container: HTMLElement) {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )
  ).filter((element) => !element.hasAttribute("inert") && element.getAttribute("aria-hidden") !== "true");
}

type WorkbenchDrawerShellProps = {
  open: boolean;
  hasRun: boolean;
  width: number;
  title: string;
  status: string;
  headerIcon: ReactNode;
  reopenIcon: ReactNode;
  reopenLabel: string;
  reopenVariant?: "pill" | "side-handle";
  closeLabel: string;
  resizeLabel: string;
  children: ReactNode;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: () => void;
  minWidth?: number;
  maxWidth?: number;
  keyboardStep?: number;
};

export function WorkbenchDrawerShell({
  open,
  hasRun,
  width,
  title,
  status,
  headerIcon,
  reopenIcon,
  reopenLabel,
  reopenVariant = "pill",
  closeLabel,
  resizeLabel,
  children,
  onWidthChange,
  onClose,
  onOpen,
  minWidth = DEFAULT_MIN_WIDTH,
  maxWidth = DEFAULT_MAX_WIDTH,
  keyboardStep = DEFAULT_KEYBOARD_STEP
}: WorkbenchDrawerShellProps) {
  const titleId = useId();
  const layerRef = useRef<HTMLDivElement | null>(null);
  const drawerRef = useRef<HTMLElement | null>(null);
  const reopenRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const resizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const [isOverlay, setIsOverlay] = useState(true);
  onCloseRef.current = onClose;

  useEffect(() => {
    const root = layerRef.current?.closest<HTMLElement>(".np-structure-workbench");
    if (!root) return;
    const update = () => setIsOverlay(root.getBoundingClientRect().width < OVERLAY_CONTAINER_WIDTH);
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(root);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    function handlePointerMove(event: PointerEvent) {
      const state = resizeStateRef.current;
      if (!state) return;
      onWidthChange(clamp(state.startWidth + state.startX - event.clientX, minWidth, maxWidth));
    }
    function stopResize() {
      resizeStateRef.current = null;
    }
    document.addEventListener("pointermove", handlePointerMove);
    document.addEventListener("pointerup", stopResize);
    document.addEventListener("pointercancel", stopResize);
    return () => {
      document.removeEventListener("pointermove", handlePointerMove);
      document.removeEventListener("pointerup", stopResize);
      document.removeEventListener("pointercancel", stopResize);
      resizeStateRef.current = null;
    };
  }, [maxWidth, minWidth, onWidthChange]);

  useEffect(() => {
    if (!open || !isOverlay) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => {
      drawerRef.current?.querySelector<HTMLElement>("button:not([disabled]), [tabindex='0']")?.focus();
    });

    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (!isOverlay || event.key !== "Tab" || !drawerRef.current) return;
      const focusable = focusableElements(drawerRef.current);
      if (!focusable.length) {
        event.preventDefault();
        drawerRef.current.focus();
        return;
      }
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

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleKeyDown);
      const restoreTarget = restoreFocusRef.current;
      window.requestAnimationFrame(() => {
        const hiddenAncestor = restoreTarget?.closest<HTMLElement>("[inert], [aria-hidden='true']");
        if (restoreTarget?.isConnected && !hiddenAncestor) restoreTarget.focus();
        else reopenRef.current?.focus();
      });
    };
  }, [isOverlay, open]);

  function startResize(event: ReactPointerEvent<HTMLDivElement>) {
    event.preventDefault();
    resizeStateRef.current = { startX: event.clientX, startWidth: width };
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const amount = event.shiftKey ? keyboardStep * 2.5 : keyboardStep;
    onWidthChange(
      clamp(width + (event.key === "ArrowLeft" ? amount : -amount), minWidth, maxWidth)
    );
  }

  const style = { "--np-sw-drawer-width": `${width}px` } as CSSProperties;

  return (
    <>
      <div
        ref={layerRef}
        className={`np-sw-drawer-layer${open ? " is-open" : ""}${isOverlay ? " is-overlay" : ""}`}
        style={style}
        aria-hidden={!open}
      >
        <button
          type="button"
          className="np-sw-drawer-backdrop"
          aria-label={`${closeLabel}背景`}
          tabIndex={open && isOverlay ? 0 : -1}
          onClick={onClose}
        />
        <aside
          ref={drawerRef}
          className="np-sw-drawer"
          role="dialog"
          aria-modal={isOverlay ? "true" : "false"}
          aria-labelledby={titleId}
          tabIndex={-1}
          inert={!open}
        >
          <div
            className="np-sw-drawer__resizer"
            role="separator"
            tabIndex={open && !isOverlay ? 0 : -1}
            aria-label={resizeLabel}
            aria-orientation="vertical"
            aria-valuemin={minWidth}
            aria-valuemax={maxWidth}
            aria-valuenow={width}
            onPointerDown={startResize}
            onKeyDown={resizeWithKeyboard}
          />
          <header className="np-sw-drawer__header">
            <div>
              <span>{headerIcon}</span>
              <div>
                <h2 id={titleId}>{title}</h2>
                <p>{status}</p>
              </div>
            </div>
            <button type="button" className="np-sw-icon-button" aria-label={closeLabel} onClick={onClose}>
              <X aria-hidden="true" />
            </button>
          </header>
          <div className="np-sw-drawer__body" aria-live="polite">
            {children}
          </div>
        </aside>
      </div>

      {hasRun && !open ? (
        <button
          ref={reopenRef}
          type="button"
          className={`np-sw-drawer-reopen${reopenVariant === "side-handle" ? " is-side-handle" : ""}`}
          onClick={onOpen}
          aria-label={reopenLabel}
          title={reopenLabel}
        >
          {reopenIcon}
          <span>{reopenLabel.replace(/^展开/, "")}</span>
        </button>
      ) : null}
    </>
  );
}
