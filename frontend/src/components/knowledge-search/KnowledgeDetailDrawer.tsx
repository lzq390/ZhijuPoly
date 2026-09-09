import { PanelRightOpen, X } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode
} from "react";
import { useMotionPresence } from "../../hooks/useMotionPresence";
import { useDrawerResize } from "../../hooks/useDrawerResize";
import { useModalFocus } from "../../hooks/useModalFocus";
import { useContentMotion } from "../../hooks/useContentMotion";

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
  widthProfile: KnowledgeDrawerWidthProfile;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: () => void;
};

type KnowledgeDrawerWidthProfile = {
  min: number;
  max: number;
  defaultWidth: number;
  keyboardStep: number;
  keyboardLargeStep: number;
};

const TWO_K_MEDIA_QUERY = "(min-width: 2000px) and (min-height: 1120px)";

const STANDARD_DRAWER_PROFILE: KnowledgeDrawerWidthProfile = {
  min: 320,
  max: 560,
  defaultWidth: 380,
  keyboardStep: 10,
  keyboardLargeStep: 40
};

const TWO_K_DRAWER_PROFILE: KnowledgeDrawerWidthProfile = {
  min: 480,
  max: 720,
  defaultWidth: 540,
  keyboardStep: 24,
  keyboardLargeStep: 72
};

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function isTwoKViewport() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(TWO_K_MEDIA_QUERY).matches
  );
}

export function useKnowledgeDrawerSizing() {
  const [isTwoK, setIsTwoK] = useState(isTwoKViewport);
  const [drawerWidth, setDrawerWidth] = useState(() =>
    isTwoKViewport() ? TWO_K_DRAWER_PROFILE.defaultWidth : STANDARD_DRAWER_PROFILE.defaultWidth
  );
  const widthProfile = isTwoK ? TWO_K_DRAWER_PROFILE : STANDARD_DRAWER_PROFILE;
  const previousProfileRef = useRef(widthProfile);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(TWO_K_MEDIA_QUERY);
    const update = () => setIsTwoK(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    const previousProfile = previousProfileRef.current;
    if (previousProfile === widthProfile) return;
    setDrawerWidth((currentWidth) => {
      const ratio = clamp(
        (currentWidth - previousProfile.min) / (previousProfile.max - previousProfile.min),
        0,
        1
      );
      return Math.round(widthProfile.min + ratio * (widthProfile.max - widthProfile.min));
    });
    previousProfileRef.current = widthProfile;
  }, [widthProfile]);

  return { drawerWidth, setDrawerWidth, widthProfile };
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
  widthProfile,
  onWidthChange,
  onClose,
  onOpen
}: KnowledgeDetailDrawerProps) {
  const presence = useMotionPresence<HTMLElement>(open, { enter: "drawerEnter", exit: "drawerExit", property: "transform" });
  const drawerRef = presence.ref;
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);
  const [everOpened, setEverOpened] = useState(open);
  const [activeTab, setActiveTab] = useState(tabs[0]?.id ?? "");
  const mobile = useMobileDrawer();
  const resize = useDrawerResize({ width, minWidth: widthProfile.min, maxWidth: widthProfile.max, onWidthChange, enabled: open && !mobile });
  useContentMotion(bodyRef, activeTab, "tab");
  useModalFocus({ active: mobile && presence.present, open, scopeRef: drawerRef, panelRef: drawerRef,
    initialFocusRef: closeButtonRef, onClose, ownerId: id });

  useEffect(() => {
    setActiveTab(tabs[0]?.id ?? "");
  }, [contentKey]);

  useEffect(() => {
    let frame = 0;
    if (presence.present && !wasOpenRef.current) {
      returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setEverOpened(true);
      frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus({ preventScroll: true }));
    }
    if (!presence.present && wasOpenRef.current) {
      frame = window.requestAnimationFrame(() => {
        const target = returnFocusRef.current;
        if (target?.isConnected && !target.closest('[inert], [aria-hidden="true"]')) target.focus({ preventScroll: true });
      });
    }
    wasOpenRef.current = presence.present;
    return () => window.cancelAnimationFrame(frame);
  }, [presence.present]);

  useEffect(() => {
    if (!open || mobile) return;

    function handleKeyDown(event: KeyboardEvent) {
      // Hidden, kept-alive knowledge modes must not consume another mode's Escape.
      let ancestor = drawerRef.current?.parentElement;
      while (ancestor) {
        if (ancestor.hidden || ancestor.getAttribute("aria-hidden") === "true" || getComputedStyle(ancestor).display === "none") return;
        ancestor = ancestor.parentElement;
      }
      if (event.key === "Escape" && !event.defaultPrevented) {
        event.preventDefault();
        onClose();
        return;
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [mobile, onClose, open]);

  function resizeWithKeyboard(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const step = event.shiftKey ? widthProfile.keyboardLargeStep : widthProfile.keyboardStep;
    onWidthChange(
      clamp(width + (event.key === "ArrowLeft" ? step : -step), widthProfile.min, widthProfile.max)
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
        className="ks-drawer-backdrop"
        data-motion-present={presence.present && mobile}
        data-motion-active={presence.active && mobile}
        data-modal-owner={id}
        aria-hidden="true"
        type="button"
        tabIndex={-1}
        aria-label="关闭详情抽屉"
        onClick={onClose}
      />

      {everOpened && !presence.present && showReopen ? (
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
        {...presence.motionProps}
        data-motion-present={presence.present}
        id={id}
        className={`ks-detail-drawer${presence.present ? " is-open" : ""}${resize.resizing ? " is-resizing" : ""}`}
        style={{ "--ks-drawer-width": `${width}px` } as CSSProperties}
        role="dialog"
        aria-modal={mobile ? "true" : undefined}
        aria-labelledby={titleId}
        aria-hidden={!open}
        inert={!open}
        tabIndex={-1}
      >
        <div
          className="ks-drawer-resizer"
          role="separator"
          tabIndex={open && !mobile ? 0 : -1}
          aria-label="调整详情抽屉宽度"
          aria-orientation="vertical"
          aria-valuemin={widthProfile.min}
          aria-valuemax={widthProfile.max}
          aria-valuenow={Math.round(width)}
          onPointerDown={resize.onPointerDown}
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

        <div ref={bodyRef} className="ks-drawer-body">{selectedTab?.content ?? children}</div>
        {footer ? <footer className="ks-drawer-footer">{footer}</footer> : null}
      </aside>
    </>
  );
}
