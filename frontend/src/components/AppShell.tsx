import type { ReactNode, RefObject } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Menu, MessageSquare } from "lucide-react";
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
  children: ReactNode;
};

type MobileSidebarDrawerProps = {
  open: boolean;
  onBackdropClick: () => void;
  children: ReactNode;
};

function MobileSidebarDrawer({
  open,
  onBackdropClick,
  children
}: MobileSidebarDrawerProps) {
  if (!open) {
    return null;
  }

  return (
    <div className="np-sidebar-mobile-layer">
      <button
        type="button"
        aria-label="关闭导航背景"
        className="np-sidebar-mobile-backdrop"
        onClick={onBackdropClick}
      />
      <aside
        id="np-mobile-navigation"
        role="dialog"
        aria-modal="true"
        aria-label="平台导航"
        className="np-sidebar-mobile-panel"
      >
        {children}
      </aside>
    </div>
  );
}

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
  children
}: AppShellProps) {
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const [isProjectExpanded, setIsProjectExpanded] = useState(false);
  const [generalSessionQuery, setGeneralSessionQuery] = useState("");
  const mobileMenuButtonRef = useRef<HTMLButtonElement | null>(null);
  const mobileCloseButtonRef = useRef<HTMLButtonElement | null>(null);
  const gpuSessionControl = useDevGpuSessionControl(DEV_GPU_SESSION_CONTROL_ENABLED);

  const isHome = activeModule === "home";
  const isReverseDesignWorkbench = activeModule === "reverseDesign";
  const isConditionalGenerationWorkbench = activeModule === "conditionalGeneration";
  const isStructureWorkbench = activeModule === "structureWorkbench";
  const isDatabaseFilterWorkbench = activeModule === "databaseFilter";
  const isDatabaseAnalysisWorkbench = activeModule === "database";
  const isKnowledgeWorkbench = activeModule === "knowledge";
  const isPolytaoWorkbench = activeModule === "polytaoGeneration";
  const isResearchWorkbench =
    activeModule === "explorer" ||
    activeModule === "databaseQuery" ||
    isDatabaseFilterWorkbench ||
    isDatabaseAnalysisWorkbench ||
    isKnowledgeWorkbench ||
    isPolytaoWorkbench ||
    isStructureWorkbench ||
    isReverseDesignWorkbench ||
    isConditionalGenerationWorkbench;

  const activeGroupId =
    moduleGroups.find((group) => group.items.some((item) => item.isActive))?.id ?? null;
  const [expandedGroupIds, setExpandedGroupIds] = useState<Set<AppShellModuleGroup["id"]>>(
    () => (activeGroupId ? new Set([activeGroupId]) : new Set())
  );

  const closeMobileMenu = useCallback((restoreFocus: boolean) => {
    setIsMobileMenuOpen(false);
    if (restoreFocus) {
      mobileMenuButtonRef.current?.focus();
    }
  }, []);

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
    if (!isMobileMenuOpen) {
      return;
    }

    mobileCloseButtonRef.current?.focus();

    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (
        event.key !== "Escape" ||
        event.defaultPrevented ||
        document.querySelector(".np-sidebar-project-menu")
      ) {
        return;
      }
      event.preventDefault();
      closeMobileMenu(true);
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [closeMobileMenu, isMobileMenuOpen]);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") {
      return;
    }

    const desktopMedia = window.matchMedia("(min-width: 1024px)");
    function handleViewportChange(event: MediaQueryListEvent) {
      if (event.matches) {
        setIsMobileMenuOpen(false);
      }
    }

    desktopMedia.addEventListener("change", handleViewportChange);
    return () => desktopMedia.removeEventListener("change", handleViewportChange);
  }, []);

  function handleNavigate(action: () => void) {
    action();
    closeMobileMenu(false);
  }

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
    onOpenHome: () => handleNavigate(onOpenHome),
    onNavigate: handleNavigate,
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
    <div className="np-app-shell">
      <aside className="np-sidebar-desktop" aria-label="平台侧边栏">
        <PlatformSidebar {...sharedSidebarProps} gpuStatusId="gpu-session-status-desktop" />
      </aside>

      <MobileSidebarDrawer
        open={isMobileMenuOpen}
        onBackdropClick={() => closeMobileMenu(true)}
      >
        <PlatformSidebar
          {...sharedSidebarProps}
          gpuStatusId="gpu-session-status-mobile"
          closeButtonRef={mobileCloseButtonRef}
          onClose={() => closeMobileMenu(true)}
        />
      </MobileSidebarDrawer>

      <div className="np-app-shell__body">
        <MobileSidebarHeader
          menuButtonRef={mobileMenuButtonRef}
          expanded={isMobileMenuOpen}
          onOpen={() => setIsMobileMenuOpen(true)}
        />

        <main
          className={
            isHome
              ? "min-h-0 flex-1 overflow-hidden"
              : isResearchWorkbench
                ? `min-h-0 flex-1 overflow-hidden ${
                    isReverseDesignWorkbench ||
                    isConditionalGenerationWorkbench ||
                    isStructureWorkbench ||
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
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
