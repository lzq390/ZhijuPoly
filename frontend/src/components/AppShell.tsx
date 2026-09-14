import type { ReactNode, RefObject } from "react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { Menu, MessageSquare } from "lucide-react";
import { ModuleTransitionContext } from "../hooks/ModuleTransitionContext";
import type { ModuleTransitionView } from "../hooks/useModuleTransition";
import { useGuardedNavigation } from "../hooks/useGuardedNavigation";
import { useModalFocus } from "../hooks/useModalFocus";
import { useMotionPresence } from "../hooks/useMotionPresence";
import { useReducedMotion } from "../hooks/useReducedMotion";
import { motionDuration, motionEasing } from "../lib/motion";
import type { OpenScienceGeneralSessionSummary } from "../lib/openScienceGeneralSessionBridge";
import type { OpenScienceProjectSummary } from "../lib/openScienceProjectBridge";
import { useDevGpuSessionControl } from "./GpuSessionButton";
import {
  PlatformSidebar,
  type AppShellModuleGroup,
  type AppShellModuleItem
} from "./sidebar/PlatformSidebar";
import "./sidebar/platform-sidebar.css";

export type { AppShellModuleGroup, AppShellModuleItem } from "./sidebar/PlatformSidebar";

const DEV_GPU_SESSION_CONTROL_ENABLED =
  import.meta.env.DEV &&
  import.meta.env.VITE_DEV_GPU_SESSION_CONTROL === "true";

type AppShellProps = {
  activeModule: string;
  fullBleed?: boolean;
  standaloneModules: AppShellModuleItem[];
  moduleGroups: AppShellModuleGroup[];
  onOpenHome: () => void;
  projects: OpenScienceProjectSummary[];
  activeProjectDirectory: string | null;
  isProjectBridgeReady: boolean;
  onOpenProject: (directory: string) => void;
  onBrowseProjects: () => void;
  onNewProject: () => void;
  onSetProjectFavorite: (directory: string, favorite: boolean) => void;
  onArchiveProject: (directory: string) => void;
  isGeneralWorkspaceActive: boolean;
  generalSessions: OpenScienceGeneralSessionSummary[];
  activeGeneralSessionID: string | null;
  isGeneralSessionBridgeReady: boolean;
  onOpenGeneralWorkspace: () => void;
  onNewGeneralSession: () => void;
  onOpenGeneralSession: (sessionID: string) => void;
  onRenameGeneralSession: (sessionID: string, title: string) => void;
  onDeleteGeneralSession: (sessionID: string) => void;
  beforeNavigate?: () => Promise<void | boolean>;
  moduleTransition?: ModuleTransitionView;
  children: ReactNode;
  recordingControls?: ReactNode;
};

const SCROLLBAR_HIDE_DELAY_MS = 700;
const SCROLLBAR_HIDDEN_THUMB_COLOR = "rgba(88, 112, 141, 0)";
const SCROLLBAR_ACTIVE_THUMB_COLOR = "rgba(88, 112, 141, 0.5)";
const noTransitionSubscription = () => () => {};
const noTransitionSnapshot = () => null;

function MobileSidebarHeader({
  menuButtonRef,
  expanded,
  onOpen
}: {
  menuButtonRef: RefObject<HTMLButtonElement | null>;
  expanded: boolean;
  onOpen: () => void;
}) {
  return (
    <header className="np-sidebar-mobile-header">
      <button
        ref={menuButtonRef}
        type="button"
        aria-label="打开导航"
        aria-controls="np-mobile-navigation"
        aria-expanded={expanded}
        className="np-sidebar-mobile-header__button"
        onClick={onOpen}
      >
        <Menu aria-hidden="true" />
      </button>
      <div className="np-sidebar-mobile-header__brand" aria-label="智聚万物">
        <span className="np-sidebar-mobile-header__mark" aria-hidden="true">
          <MessageSquare />
        </span>
        <span>智聚万物</span>
      </div>
      <span className="np-sidebar-mobile-header__spacer" aria-hidden="true" />
    </header>
  );
}

export function AppShell({
  activeModule,
  fullBleed = false,
  standaloneModules,
  moduleGroups,
  onOpenHome,
  projects,
  activeProjectDirectory,
  isProjectBridgeReady,
  onOpenProject,
  onBrowseProjects,
  onNewProject,
  onSetProjectFavorite,
  onArchiveProject,
  isGeneralWorkspaceActive,
  generalSessions,
  activeGeneralSessionID,
  isGeneralSessionBridgeReady,
  onOpenGeneralWorkspace,
  onNewGeneralSession,
  onOpenGeneralSession,
  onRenameGeneralSession,
  onDeleteGeneralSession,
  beforeNavigate,
  recordingControls,
  moduleTransition,
  children
}: AppShellProps) {
  useSyncExternalStore(moduleTransition?.subscribe ?? noTransitionSubscription,
    moduleTransition?.getSnapshot ?? noTransitionSnapshot, moduleTransition?.getSnapshot ?? noTransitionSnapshot);
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const [isProjectExpanded, setIsProjectExpanded] = useState(false);
  const [generalSessionQuery, setGeneralSessionQuery] = useState("");
  const mobileMenuButtonRef = useRef<HTMLButtonElement | null>(null);
  const mobileCloseButtonRef = useRef<HTMLButtonElement | null>(null);
  const appShellRef = useRef<HTMLDivElement | null>(null);
  const localMainRef = useRef<HTMLElement | null>(null);
  const localContentRef = useRef<HTMLDivElement | null>(null);
  const mainRef = moduleTransition?.mainRef ?? localMainRef;
  const contentRef = moduleTransition?.contentRef ?? localContentRef;
  const mobileLayerRef = useRef<HTMLDivElement | null>(null);
  const mobilePresence = useMotionPresence<HTMLElement>(isMobileMenuOpen, { enter: "drawerEnter", exit: "drawerExit", property: "transform" });
  const mobileFocusDestination = useRef<"menu" | "content">("menu");
  const wasMobilePresent = useRef(false);
  const reducedMotion = useReducedMotion();
  const fallbackNavigation = useGuardedNavigation({
    beforeNavigate: moduleTransition ? undefined : beforeNavigate, activeModule, containerRef: appShellRef,
    onCommit: () => {
      mobileFocusDestination.current = "content";
      setIsMobileMenuOpen(false);
    }
  });
  const navigation = moduleTransition ?? fallbackNavigation;
  const handleNavigate = moduleTransition ? (action: () => void) => action() : fallbackNavigation.navigate;
  useLayoutEffect(() => {
    moduleTransition?.setCovered(mobilePresence.present);
  }, [moduleTransition?.setCovered, mobilePresence.present]);
  useLayoutEffect(() => {
    if (!moduleTransition?.exitRevision) return;
    mobileFocusDestination.current = "content";
    setIsMobileMenuOpen(false);
  }, [moduleTransition?.exitRevision]);
  const scrollbarHideTimersRef = useRef<Map<HTMLElement, number>>(new Map());
  const scrollbarAnimationsRef = useRef<Map<HTMLElement, Animation>>(new Map());
  const gpuSessionControl = useDevGpuSessionControl(DEV_GPU_SESSION_CONTROL_ENABLED);

  const isHome = activeModule === "home";
  const isReverseDesignWorkbench = activeModule === "reverseDesign";
  const isConditionalGenerationWorkbench = activeModule === "conditionalGeneration";
  const isStructureWorkbench = activeModule === "structureWorkbench";
  const isHomopolymerPredictionWorkbench = activeModule === "homopolymerPrediction";
  const isMonomerPolymerizationWorkbench = activeModule === "monomerPolymerization";
  const isMdSimulationWorkbench = activeModule === "mdSimulationDemo";
  const isMonomerMdSimulationWorkbench = activeModule === "monomerMdSimulation";
  const isMonomerDftWorkbench = activeModule === "monomerDft";
  const isHighThroughputWorkbench = activeModule === "highThroughputWorkflowDemo";
  const isSimilarityExplorerWorkbench = activeModule === "explorer";
  const isDatabaseQueryWorkbench = activeModule === "databaseQuery";
  const isDatabaseFilterWorkbench = activeModule === "databaseFilter";
  const isDatabaseAnalysisWorkbench = activeModule === "database";
  const isKnowledgeWorkbench = activeModule === "knowledge";
  const isPolytaoWorkbench = activeModule === "polytaoGeneration";
  const isResearchWorkbench =
    isSimilarityExplorerWorkbench ||
    isDatabaseQueryWorkbench ||
    isDatabaseFilterWorkbench ||
    isDatabaseAnalysisWorkbench ||
    isKnowledgeWorkbench ||
    isPolytaoWorkbench ||
    isHomopolymerPredictionWorkbench ||
    isMonomerPolymerizationWorkbench ||
    isMdSimulationWorkbench ||
    isMonomerMdSimulationWorkbench ||
    isMonomerDftWorkbench ||
    isHighThroughputWorkbench ||
    isStructureWorkbench ||
    isReverseDesignWorkbench ||
    isConditionalGenerationWorkbench;

  const activeGroupId =
    moduleGroups.find((group) => group.items.some((item) => item.isActive))?.id ?? null;
  const [expandedGroupIds, setExpandedGroupIds] = useState<Set<AppShellModuleGroup["id"]>>(
    () => (activeGroupId ? new Set([activeGroupId]) : new Set())
  );

  const closeMobileMenu = useCallback((restoreFocus: boolean) => {
    if (restoreFocus || !moduleTransition) navigation.cancel();
    mobileFocusDestination.current = restoreFocus ? "menu" : "content";
    setIsMobileMenuOpen(false);
  }, [navigation.cancel, Boolean(moduleTransition)]);

  useModalFocus({
    active: mobilePresence.present, open: isMobileMenuOpen,
    scopeRef: mobileLayerRef, panelRef: mobilePresence.ref,
    initialFocusRef: mobileCloseButtonRef, ownerId: "np-mobile-navigation",
    onClose: () => closeMobileMenu(true)
  });

  useEffect(() => {
    if (!activeGroupId) {
      return;
    }

    setExpandedGroupIds((current) => {
      if (current.has(activeGroupId)) {
        return current;
      }
      const next = new Set(current);
      next.add(activeGroupId);
      return next;
    });
  }, [activeGroupId]);

  useEffect(() => {
    if (isGeneralWorkspaceActive) {
      setIsProjectExpanded(false);
    }
  }, [isGeneralWorkspaceActive]);

  useEffect(() => {
    if (!mobilePresence.present && wasMobilePresent.current &&
        (mobileFocusDestination.current === "menu" || !moduleTransition || moduleTransition.phase === "idle")) {
      const target = mobileFocusDestination.current === "menu" ? mobileMenuButtonRef.current : mainRef.current;
      target?.focus({ preventScroll: true });
    }
    wasMobilePresent.current = mobilePresence.present;
  }, [mobilePresence.present, moduleTransition?.phase]);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return;
    }

    const desktopMedia = window.matchMedia("(min-width: 1024px)");
    function handleViewportChange(event: MediaQueryListEvent) {
      if (event.matches) {
        closeMobileMenu(false);
      }
    }

    desktopMedia.addEventListener("change", handleViewportChange);
    return () => desktopMedia.removeEventListener("change", handleViewportChange);
  }, [closeMobileMenu]);

  useEffect(() => {
    const appShell = appShellRef.current;
    if (!appShell) return;

    const scrollbarHideTimers = scrollbarHideTimersRef.current;
    const scrollbarAnimations = scrollbarAnimationsRef.current;

    function setScrollbarVisible(scrollRegion: HTMLElement, visible: boolean) {
      const currentColor = window
        .getComputedStyle(scrollRegion)
        .getPropertyValue("--np-scrollbar-thumb-color")
        .trim();
      const previousAnimation = scrollbarAnimations.get(scrollRegion);
      if (previousAnimation) {
        scrollbarAnimations.delete(scrollRegion);
        previousAnimation.cancel();
      }

      if (visible) {
        scrollRegion.setAttribute("data-scrollbar-active", "true");
      } else {
        scrollRegion.removeAttribute("data-scrollbar-active");
      }

      if (reducedMotion || typeof scrollRegion.animate !== "function") return;
      const targetColor = visible
        ? SCROLLBAR_ACTIVE_THUMB_COLOR
        : SCROLLBAR_HIDDEN_THUMB_COLOR;
      const startColor = currentColor && currentColor !== "auto"
        ? currentColor
        : visible
          ? SCROLLBAR_HIDDEN_THUMB_COLOR
          : SCROLLBAR_ACTIVE_THUMB_COLOR;
      if (startColor === targetColor) return;

      try {
        const animation = scrollRegion.animate(
          [
            { "--np-scrollbar-thumb-color": startColor },
            { "--np-scrollbar-thumb-color": targetColor }
          ],
          {
            duration: motionDuration(scrollRegion, visible ? "enter" : "exit"),
            easing: motionEasing(scrollRegion, visible ? "enter" : "exit"),
            fill: "both"
          }
        );
        scrollbarAnimations.set(scrollRegion, animation);
        animation.onfinish = () => {
          if (scrollbarAnimations.get(scrollRegion) !== animation) return;
          scrollbarAnimations.delete(scrollRegion);
          animation.cancel();
        };
      } catch {
        // The data attribute still provides an immediate fallback in browsers
        // that expose Web Animations but cannot animate registered custom properties.
      }
    }

    function handleScroll(event: Event) {
      const scrollRegion = event.target;
      if (!(scrollRegion instanceof HTMLElement)) return;

      if (!scrollRegion.hasAttribute("data-scrollbar-active")) {
        setScrollbarVisible(scrollRegion, true);
      }
      const previousTimer = scrollbarHideTimers.get(scrollRegion);
      if (previousTimer !== undefined) window.clearTimeout(previousTimer);

      const nextTimer = window.setTimeout(() => {
        setScrollbarVisible(scrollRegion, false);
        scrollbarHideTimers.delete(scrollRegion);
      }, SCROLLBAR_HIDE_DELAY_MS);
      scrollbarHideTimers.set(scrollRegion, nextTimer);
    }

    appShell.addEventListener("scroll", handleScroll, { capture: true, passive: true });
    return () => {
      appShell.removeEventListener("scroll", handleScroll, true);
      scrollbarHideTimers.forEach((timer, scrollRegion) => {
        window.clearTimeout(timer);
        scrollRegion.removeAttribute("data-scrollbar-active");
      });
      scrollbarHideTimers.clear();
      scrollbarAnimations.forEach((animation) => animation.cancel());
      scrollbarAnimations.clear();
    };
  }, [reducedMotion]);

  function handleToggleGroup(groupId: AppShellModuleGroup["id"]) {
    setExpandedGroupIds((current) => {
      const next = new Set(current);
      if (next.has(groupId)) {
        next.delete(groupId);
      } else {
        next.add(groupId);
      }
      return next;
    });
  }

  const sharedSidebarProps = {
    standaloneModules,
    moduleGroups,
    onOpenHome: () => handleNavigate(onOpenHome, { id: "home", label: "首页" }),
    onNavigate: handleNavigate,
    pendingTarget: navigation.pendingTarget,
    showPending: navigation.showPending,
    expandedGroupIds,
    onToggleGroup: handleToggleGroup,
    projects,
    activeProjectDirectory,
    isProjectBridgeReady,
    onOpenProject: (directory: string) => handleNavigate(() => onOpenProject(directory)),
    onBrowseProjects: () => handleNavigate(onBrowseProjects),
    onNewProject: () => handleNavigate(onNewProject),
    onSetProjectFavorite,
    onArchiveProject,
    isProjectExpanded,
    onProjectExpandedChange: setIsProjectExpanded,
    isGeneralWorkspaceActive,
    generalSessions,
    activeGeneralSessionID,
    isGeneralSessionBridgeReady,
    onOpenGeneralWorkspace: () => handleNavigate(onOpenGeneralWorkspace),
    onNewGeneralSession: () => handleNavigate(onNewGeneralSession),
    onOpenGeneralSession: (sessionID: string) =>
      handleNavigate(() => onOpenGeneralSession(sessionID)),
    onRenameGeneralSession,
    onDeleteGeneralSession,
    generalSessionQuery,
    onGeneralSessionQueryChange: setGeneralSessionQuery,
    gpuSessionControl: DEV_GPU_SESSION_CONTROL_ENABLED ? gpuSessionControl : null
  };

  return (
    <div ref={appShellRef} className="np-app-shell">
      <aside className="np-sidebar-desktop" aria-label="平台侧边栏">
        <PlatformSidebar {...sharedSidebarProps} gpuStatusId="gpu-session-status-desktop" />
      </aside>

      {mobilePresence.present ? (
        <div ref={mobileLayerRef} className="np-sidebar-mobile-layer" data-motion-active={mobilePresence.active}>
          <button type="button" aria-label="关闭导航背景" tabIndex={-1}
            className="np-sidebar-mobile-backdrop" onClick={() => closeMobileMenu(true)} />
          <aside ref={mobilePresence.ref} {...mobilePresence.motionProps}
            id="np-mobile-navigation" role="dialog" aria-modal="true" aria-label="平台导航"
            tabIndex={-1} className="np-sidebar-mobile-panel">
            <PlatformSidebar {...sharedSidebarProps} gpuStatusId="gpu-session-status-mobile"
              closeButtonRef={mobileCloseButtonRef} onClose={() => closeMobileMenu(true)} />
          </aside>
        </div>
      ) : null}

      <div className="np-app-shell__body">
        <MobileSidebarHeader
          menuButtonRef={mobileMenuButtonRef}
          expanded={isMobileMenuOpen}
          onOpen={() => setIsMobileMenuOpen(true)}
        />

        {recordingControls}
        <main
          ref={mainRef}
          tabIndex={-1}
          aria-busy={moduleTransition ? moduleTransition.phase !== "idle" : undefined}
          className={
            isHome
              ? "min-h-0 flex-1 overflow-hidden"
              : isResearchWorkbench
                ? `min-h-0 flex-1 overflow-hidden ${
                    isReverseDesignWorkbench ||
                    isConditionalGenerationWorkbench ||
                    isStructureWorkbench ||
                    isHomopolymerPredictionWorkbench ||
                    isMonomerPolymerizationWorkbench ||
                    isMdSimulationWorkbench ||
                    isMonomerMdSimulationWorkbench ||
                    isMonomerDftWorkbench ||
                    isHighThroughputWorkbench ||
                    isSimilarityExplorerWorkbench ||
                    isDatabaseQueryWorkbench ||
                    isDatabaseFilterWorkbench ||
                    isDatabaseAnalysisWorkbench ||
                    isKnowledgeWorkbench ||
                    isPolytaoWorkbench
                      ? "p-0"
                      : "py-5 md:py-8"
                  }`
                : "flex-1 overflow-y-auto px-4 py-5 md:px-8 md:py-8"
          }
        >
          <div
            ref={contentRef}
            data-module-content={activeModule}
            data-module-phase={moduleTransition?.phase ?? "idle"}
            className={
              isHome
                ? "h-full"
                : [
                    "relative mx-auto flex flex-col",
                    isResearchWorkbench ? "h-full gap-0" : "gap-8",
                    fullBleed ? "max-w-none" : "max-w-[1480px]"
                  ].join(" ")
            }
          >
            <ModuleTransitionContext.Provider value={moduleTransition?.blocked ?? false}>
              {children}
            </ModuleTransitionContext.Provider>
          </div>
        </main>
      </div>
    </div>
  );
}
