import type { KeyboardEvent, ReactNode } from "react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Archive, ChevronDown, Ellipsis, Folder, Menu, MessageSquare, Plus, Search, Star, X } from "lucide-react";
import type { OpenScienceProjectSummary } from "../lib/openScienceProjectBridge";

export type AppShellModuleItem = {
  id: string;
  label: string;
  description: string;
  route: string;
  icon: ReactNode;
  isActive: boolean;
  onClick: () => void;
};

export type AppShellModuleGroup = {
  title: string;
  items: AppShellModuleItem[];
};

type AppShellProps = {
  activeModule: string;
  fullBleed?: boolean;
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
  children: ReactNode;
};

export function AppShell({
  activeModule,
  fullBleed = false,
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
  children
}: AppShellProps) {
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const isHome = activeModule === "home";
  const isResearchWorkbench = activeModule === "explorer" || activeModule === "databaseQuery";
  const activeGroupTitle =
    moduleGroups.find((group) => group.items.some((item) => item.isActive))?.title ?? null;
  const [expandedGroupTitle, setExpandedGroupTitle] = useState<string | null>(activeGroupTitle);

  useEffect(() => {
    setExpandedGroupTitle(activeGroupTitle);
  }, [activeModule, activeGroupTitle]);

  function handleNavigate(action: () => void) {
    action();
    setIsMobileMenuOpen(false);
  }

  return (
    <div className="flex h-screen overflow-hidden bg-[#f4f6f8] text-slate-950">
      <aside className="hidden w-[276px] shrink-0 border-r border-slate-200/80 bg-[#f7f8fa] px-2.5 py-2 lg:block">
        <SidebarContent
          moduleGroups={moduleGroups}
          onOpenHome={() => handleNavigate(onOpenHome)}
          onNavigate={handleNavigate}
          expandedGroupTitle={expandedGroupTitle}
          onToggleGroup={(title) =>
            setExpandedGroupTitle((current) => (current === title ? null : title))
          }
          projects={projects}
          activeProjectDirectory={activeProjectDirectory}
          isProjectBridgeReady={isProjectBridgeReady}
          onOpenProject={(directory) => handleNavigate(() => onOpenProject(directory))}
          onBrowseProjects={() => handleNavigate(onBrowseProjects)}
          onNewProject={() => handleNavigate(onNewProject)}
          onSetProjectFavorite={onSetProjectFavorite}
          onArchiveProject={onArchiveProject}
        />
      </aside>

      {isMobileMenuOpen ? (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            type="button"
            aria-label="关闭导航"
            className="absolute inset-0 bg-slate-950/30"
            onClick={() => setIsMobileMenuOpen(false)}
          />
          <aside className="relative h-full w-[86vw] max-w-[340px] border-r border-slate-200 bg-[#f7f8fa] px-2.5 py-2 shadow-2xl">
            <SidebarContent
              moduleGroups={moduleGroups}
              onOpenHome={() => handleNavigate(onOpenHome)}
              onNavigate={handleNavigate}
              expandedGroupTitle={expandedGroupTitle}
              onToggleGroup={(title) =>
                setExpandedGroupTitle((current) => (current === title ? null : title))
              }
              projects={projects}
              activeProjectDirectory={activeProjectDirectory}
              isProjectBridgeReady={isProjectBridgeReady}
              onOpenProject={(directory) => handleNavigate(() => onOpenProject(directory))}
              onBrowseProjects={() => handleNavigate(onBrowseProjects)}
              onNewProject={() => handleNavigate(onNewProject)}
              onSetProjectFavorite={onSetProjectFavorite}
              onArchiveProject={onArchiveProject}
              trailing={
                <button
                  type="button"
                  aria-label="关闭导航"
                  className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 hover:bg-slate-200/70 hover:text-slate-950"
                  onClick={() => setIsMobileMenuOpen(false)}
                >
                  <X className="h-4 w-4" />
                </button>
              }
            />
          </aside>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b border-slate-200/80 bg-white/86 px-3 backdrop-blur lg:hidden">
          <button
            type="button"
            aria-label="打开导航"
            className="inline-flex h-10 w-10 items-center justify-center rounded-xl text-slate-700 hover:bg-slate-100"
            onClick={() => setIsMobileMenuOpen(true)}
          >
            <Menu className="h-5 w-5" />
          </button>
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <MessageSquare className="h-4 w-4 text-teal-600" />
            智聚万物
          </div>
          <div className="h-10 w-10" />
        </header>

        <main className={isHome ? "min-h-0 flex-1 overflow-hidden" : isResearchWorkbench ? "min-h-0 flex-1 overflow-hidden py-5 md:py-8" : "flex-1 overflow-y-auto px-4 py-5 md:px-8 md:py-8"}>
          <div className={isHome ? "h-full" : ["relative mx-auto flex flex-col", isResearchWorkbench ? "h-full gap-0" : "gap-8", fullBleed ? "max-w-none" : "max-w-[1480px]"].join(" ")}>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}

type SidebarContentProps = {
  moduleGroups: AppShellModuleGroup[];
  onOpenHome: () => void;
  onNavigate: (action: () => void) => void;
  expandedGroupTitle: string | null;
  onToggleGroup: (title: string) => void;
  projects: OpenScienceProjectSummary[];
  activeProjectDirectory: string | null;
  isProjectBridgeReady: boolean;
  onOpenProject: (directory: string) => void;
  onBrowseProjects: () => void;
  onNewProject: () => void;
  onSetProjectFavorite: (directory: string, favorite: boolean) => void;
  onArchiveProject: (directory: string) => void;
  trailing?: ReactNode;
};

function SidebarContent({
  moduleGroups,
  onOpenHome,
  onNavigate,
  expandedGroupTitle,
  onToggleGroup,
  projects,
  activeProjectDirectory,
  isProjectBridgeReady,
  onOpenProject,
  onBrowseProjects,
  onNewProject,
  onSetProjectFavorite,
  onArchiveProject,
  trailing
}: SidebarContentProps) {
  return (
    <div className="flex h-full flex-col gap-1.5">
      <div className="flex items-center justify-between px-1.5 py-1">
        <button type="button" className="flex min-w-0 items-center gap-2 text-left" onClick={onOpenHome}>
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-slate-950 text-white shadow-sm">
            <MessageSquare className="h-4 w-4" />
          </span>
          <span className="block min-w-0 truncate text-sm font-semibold text-slate-950">智聚万物</span>
        </button>
        {trailing}
      </div>

      <nav className="max-h-[55%] shrink-0 overflow-y-auto pr-0.5" aria-label="业务模块">
        <div className="flex flex-col gap-1 pb-2 pt-1">
          {moduleGroups.map((group) => {
            const isExpanded = expandedGroupTitle === group.title;

            return (
              <section key={group.title} className="space-y-0.5">
                <button
                  type="button"
                  aria-expanded={isExpanded}
                  className={[
                    "flex w-full items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left text-xs font-semibold uppercase tracking-[0.08em] transition-colors",
                    isExpanded
                      ? "bg-slate-200/70 text-slate-800"
                      : "text-slate-600 hover:bg-slate-200/60 hover:text-slate-900"
                  ].join(" ")}
                  onClick={() => onToggleGroup(group.title)}
                >
                  <span className="truncate">{group.title}</span>
                  <ChevronDown
                    aria-hidden="true"
                    className={[
                      "h-3.5 w-3.5 shrink-0 transition-transform",
                      isExpanded ? "rotate-0" : "-rotate-90"
                    ].join(" ")}
                  />
                </button>

                {isExpanded ? (
                  <div className="space-y-0.5 pl-1">
                    {group.items.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className={[
                          "group flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors",
                          item.isActive
                            ? "bg-white text-slate-950 shadow-sm ring-1 ring-slate-200"
                            : "text-slate-600 hover:bg-white/78 hover:text-slate-950"
                        ].join(" ")}
                        onClick={() => onNavigate(item.onClick)}
                      >
                        <span
                          className={[
                            "flex h-6 w-6 shrink-0 items-center justify-center rounded-md border transition-colors",
                            item.isActive
                              ? "border-teal-200 bg-teal-50 text-teal-700"
                              : "border-slate-200 bg-white/70 text-slate-500 group-hover:text-teal-700"
                          ].join(" ")}
                        >
                          {item.icon}
                        </span>
                        <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{item.label}</span>
                      </button>
                    ))}
                  </div>
                ) : null}
              </section>
            );
          })}
        </div>
      </nav>

      <section className="flex min-h-0 flex-1 flex-col border-t border-slate-200/80 pt-2" aria-labelledby="project-list-title">
        <div className="flex shrink-0 items-center gap-2 px-2 py-1.5">
          <Folder className="h-3.5 w-3.5 text-slate-500" aria-hidden="true" />
          <h2 id="project-list-title" className="text-xs font-semibold uppercase tracking-[0.08em] text-slate-700">
            项目
          </h2>
          <div className="ml-auto flex items-center gap-1">
            {isProjectBridgeReady ? (
              <span className="px-1 text-[11px] tabular-nums text-slate-400">{projects.length}</span>
            ) : null}
            <button
              type="button"
              aria-label="新建项目"
              title="新建项目"
              disabled={!isProjectBridgeReady}
              className="inline-flex h-6 w-6 items-center justify-center rounded-md text-slate-500 transition-colors hover:bg-white hover:text-teal-700 disabled:pointer-events-none disabled:opacity-35"
              onClick={onNewProject}
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        </div>

        <div className="shrink-0 px-1 pb-1">
          <button
            type="button"
            disabled={!isProjectBridgeReady}
            className="flex h-8 w-full items-center gap-2 rounded-lg px-2 text-left text-[13px] font-medium text-slate-600 transition-colors hover:bg-white/78 hover:text-slate-950 disabled:pointer-events-none disabled:opacity-40"
            onClick={onBrowseProjects}
          >
            <Search className="h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
            <span className="truncate">搜索项目</span>
          </button>
        </div>

        <div className="project-list-scrollbar min-h-0 flex-1 overflow-y-auto pr-0.5">
          {!isProjectBridgeReady ? (
            <p className="px-2 py-2 text-xs text-slate-400">正在同步项目…</p>
          ) : projects.length === 0 ? (
            <p className="px-2 py-2 text-xs text-slate-400">暂无项目</p>
          ) : (
            <div className="space-y-0.5 pb-2">
              {projects.map((project) => (
                <ProjectListItem
                  key={project.directory}
                  project={project}
                  active={project.directory === activeProjectDirectory}
                  onOpen={() => onOpenProject(project.directory)}
                  onSetFavorite={(favorite) => onSetProjectFavorite(project.directory, favorite)}
                  onArchive={() => onArchiveProject(project.directory)}
                />
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

const PROJECT_MENU_WIDTH = 176;
const PROJECT_MENU_HEIGHT = 88;
const PROJECT_MENU_GAP = 4;
const PROJECT_MENU_MARGIN = 8;

function ProjectListItem({
  project,
  active,
  onOpen,
  onSetFavorite,
  onArchive
}: {
  project: OpenScienceProjectSummary;
  active: boolean;
  onOpen: () => void;
  onSetFavorite: (favorite: boolean) => void;
  onArchive: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState({ top: PROJECT_MENU_MARGIN, left: PROJECT_MENU_MARGIN });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  function positionMenu() {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) {
      return;
    }

    const maxLeft = Math.max(PROJECT_MENU_MARGIN, window.innerWidth - PROJECT_MENU_WIDTH - PROJECT_MENU_MARGIN);
    const left = Math.min(Math.max(PROJECT_MENU_MARGIN, rect.right - PROJECT_MENU_WIDTH), maxLeft);
    const below = rect.bottom + PROJECT_MENU_GAP;
    const above = rect.top - PROJECT_MENU_GAP - PROJECT_MENU_HEIGHT;
    const top =
      below + PROJECT_MENU_HEIGHT <= window.innerHeight - PROJECT_MENU_MARGIN
        ? below
        : Math.max(PROJECT_MENU_MARGIN, above);

    setMenuPosition({ top, left });
  }

  function closeMenu(restoreFocus = false) {
    setMenuOpen(false);
    if (restoreFocus) {
      triggerRef.current?.focus();
    }
  }

  useLayoutEffect(() => {
    if (!menuOpen) {
      return;
    }

    positionMenu();
    menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();

    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node | null;
      if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) {
        return;
      }
      closeMenu();
    }

    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") {
        return;
      }
      event.preventDefault();
      closeMenu(true);
    }

    function handleViewportChange() {
      closeMenu();
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("scroll", handleViewportChange, true);

    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", handleViewportChange);
      window.removeEventListener("scroll", handleViewportChange, true);
    };
  }, [menuOpen]);

  function handleMenuKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Home" && event.key !== "End") {
      return;
    }

    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
    if (items.length === 0) {
      return;
    }

    event.preventDefault();
    const current = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Home") {
      items[0]?.focus();
      return;
    }
    if (event.key === "End") {
      items.at(-1)?.focus();
      return;
    }

    const step = event.key === "ArrowDown" ? 1 : -1;
    const next = current < 0 ? 0 : (current + step + items.length) % items.length;
    items[next]?.focus();
  }

  const menu = menuOpen
    ? createPortal(
        <div
          id={menuId}
          ref={menuRef}
          role="menu"
          aria-label={`${project.name} 项目操作`}
          className="fixed z-[70] w-44 rounded-xl border border-slate-200 bg-white p-1.5 shadow-[0_16px_40px_rgba(8,17,31,0.18)]"
          style={{ top: menuPosition.top, left: menuPosition.left }}
          onKeyDown={handleMenuKeyDown}
        >
          <button
            type="button"
            role="menuitem"
            className="flex min-h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[13px] font-medium text-slate-700 transition-colors hover:bg-slate-100 hover:text-slate-950 focus-visible:bg-slate-100 focus-visible:outline-none"
            onClick={() => {
              closeMenu(true);
              onSetFavorite(!project.favorite);
            }}
          >
            <Star
              className={[
                "h-3.5 w-3.5 shrink-0",
                project.favorite ? "fill-amber-400 text-amber-500" : "text-slate-400"
              ].join(" ")}
              aria-hidden="true"
            />
            {project.favorite ? "取消收藏" : "收藏项目"}
          </button>
          <button
            type="button"
            role="menuitem"
            className="flex min-h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[13px] font-medium text-slate-700 transition-colors hover:bg-slate-100 hover:text-slate-950 focus-visible:bg-slate-100 focus-visible:outline-none"
            onClick={() => {
              closeMenu();
              onArchive();
            }}
          >
            <Archive className="h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
            归档项目
          </button>
        </div>,
        document.body
      )
    : null;

  return (
    <div
      className={[
        "project-list-item group relative flex w-full items-stretch rounded-lg transition-colors",
        active
          ? "bg-white text-slate-950 shadow-sm ring-1 ring-slate-200"
          : "text-slate-600 hover:bg-white/78 hover:text-slate-950"
      ].join(" ")}
    >
      <button
        type="button"
        data-project-directory={project.directory}
        aria-current={active ? "page" : undefined}
        title={project.directory}
        className="flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-1.5 pr-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-teal-500/50"
        onClick={onOpen}
      >
        <Folder
          className={[
            "h-4 w-4 shrink-0",
            active ? "text-teal-700" : "text-slate-400 group-hover:text-teal-700"
          ].join(" ")}
          aria-hidden="true"
        />
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-1">
            <span className="block min-w-0 flex-1 truncate text-[13px] font-medium">{project.name}</span>
            {project.favorite ? (
              <Star className="h-3 w-3 shrink-0 fill-amber-400 text-amber-500" aria-hidden="true" />
            ) : null}
          </span>
          <span className="block truncate text-[11px] text-slate-400">{project.displayPath}</span>
        </span>
      </button>
      <button
        ref={triggerRef}
        type="button"
        aria-label={`打开 ${project.name} 项目菜单`}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-controls={menuOpen ? menuId : undefined}
        data-open={menuOpen ? "true" : "false"}
        className="project-actions-trigger my-1 mr-1 inline-flex w-7 shrink-0 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500/50"
        onClick={() => setMenuOpen((current) => !current)}
      >
        <Ellipsis className="h-4 w-4" aria-hidden="true" />
      </button>
      {menu}
    </div>
  );
}
