import { X } from "lucide-react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode
} from "react";
import { useMotionPresence } from "../../hooks/useMotionPresence";
import { useDrawerResize } from "../../hooks/useDrawerResize";
import { useModalFocus } from "../../hooks/useModalFocus";
import { useDrawerMode } from "../../hooks/useDrawerMode";

const DEFAULT_MIN_WIDTH = 320;
const DEFAULT_MAX_WIDTH = 560;
const DEFAULT_KEYBOARD_STEP = 16;
const OVERLAY_CONTAINER_WIDTH = 1280;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
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
  onOpen: (trigger?: HTMLElement) => void;
  minWidth?: number;
  maxWidth?: number;
  keyboardStep?: number;
  overlayContainerWidth?: number;
  restoreFocusTarget?: HTMLElement | null;
  drawerClassName?: string;
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
  keyboardStep = DEFAULT_KEYBOARD_STEP,
  overlayContainerWidth = OVERLAY_CONTAINER_WIDTH,
  restoreFocusTarget = null,
  drawerClassName = ""
}: WorkbenchDrawerShellProps) {
  const titleId = useId();
  const layerRef = useRef<HTMLDivElement | null>(null);
  const presence = useMotionPresence<HTMLElement>(open, { enter: "drawerEnter", exit: "drawerExit", property: "transform" });
  const drawerRef = presence.ref;
  const reopenRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const reopenTriggerRef = useRef<HTMLElement | null>(null);
  const restoreFocusFrameRef = useRef<number | null>(null);
  const wasPresent = useRef(false);
  const mode = useDrawerMode(layerRef, { closest: ".np-structure-workbench", inlineMinWidth: overlayContainerWidth });
  const isOverlay = mode === "overlay";
  const resize = useDrawerResize({ width, minWidth, maxWidth, onWidthChange, enabled: open && !isOverlay });
  useModalFocus({ active: presence.present && isOverlay, open, scopeRef: layerRef, panelRef: drawerRef, onClose, global: false });

  // A drag or responsive mode change is an immediate layout operation, never a
  // second position transition trailing behind the pointer/new viewport.
  useLayoutEffect(() => { if (resize.resizing) presence.finish(); }, [resize.resizing, presence.finish]);
  useLayoutEffect(() => { presence.finish(); }, [mode, presence.finish]);

  useEffect(() => {
    if (restoreFocusFrameRef.current !== null) {
      window.cancelAnimationFrame(restoreFocusFrameRef.current);
      restoreFocusFrameRef.current = null;
    }
    if (presence.present && !wasPresent.current) {
      restoreFocusRef.current = reopenTriggerRef.current
        ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
      reopenTriggerRef.current = null;
    }
    if (!presence.present && wasPresent.current) {
      const restoreTarget = restoreFocusRef.current;
      restoreFocusFrameRef.current = window.requestAnimationFrame(() => {
        restoreFocusFrameRef.current = null;
        const hiddenAncestor = restoreTarget?.closest<HTMLElement>("[inert], [aria-hidden='true']");
        if (restoreTarget?.isConnected && restoreTarget !== document.body && !hiddenAncestor) restoreTarget.focus({ preventScroll: true });
        else reopenRef.current?.focus({ preventScroll: true });
      });
    }
    wasPresent.current = presence.present;
    return () => { if (restoreFocusFrameRef.current !== null) window.cancelAnimationFrame(restoreFocusFrameRef.current); };
  }, [presence.present]);

  useEffect(() => {
    if (open && restoreFocusTarget) restoreFocusRef.current = restoreFocusTarget;
  }, [open, restoreFocusTarget]);

  useEffect(() => {
    if (!open || isOverlay) return;
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !event.defaultPrevented && drawerRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        onClose();
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOverlay, open, onClose, drawerRef]);

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
        className={`np-sw-drawer-layer${presence.present ? " is-open" : ""}${isOverlay ? " is-overlay" : ""}${resize.resizing && open ? " is-resizing" : ""}`}
        data-motion-present={presence.present}
        data-motion-active={presence.active}
        data-motion-phase={presence.phase}
        data-drawer-mode={mode}
        style={style}
        aria-hidden={!presence.present}
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
          {...presence.motionProps}
          className={`np-sw-drawer${drawerClassName ? ` ${drawerClassName}` : ""}`}
          role="dialog"
          aria-modal={isOverlay ? "true" : "false"}
          aria-labelledby={titleId}
          aria-hidden={!open}
          tabIndex={-1}
          inert={open ? undefined : true}
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
            onPointerDown={resize.onPointerDown}
            onKeyDown={resizeWithKeyboard}
          />
          <header className="np-sw-drawer__header">
            <div>
              <span>{headerIcon}</span>
              <div>
                <h2 id={titleId}>{title}</h2>
                <p role="status" aria-live="polite" aria-atomic="true">{status}</p>
              </div>
            </div>
            <button type="button" className="np-sw-icon-button" aria-label={closeLabel} onClick={onClose}>
              <X aria-hidden="true" />
            </button>
          </header>
          <div className="np-sw-drawer__body">
            {children}
          </div>
        </aside>
      </div>

      {hasRun && !presence.present ? (
        <button
          ref={reopenRef}
          type="button"
          className={`np-sw-drawer-reopen${reopenVariant === "side-handle" ? " is-side-handle" : ""}`}
          onClick={(event) => {
            reopenTriggerRef.current = event.currentTarget;
            onOpen(event.currentTarget);
          }}
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
