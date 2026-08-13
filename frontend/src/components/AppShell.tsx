import type { KeyboardEvent, ReactNode } from "react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Archive,
  ChevronDown,
  Ellipsis,
  Folder,
  Menu,
  MessageSquare,
  Plus,
  Search,
  Star,
  Trash2,
  X
} from "lucide-react";
import type { OpenScienceGeneralSessionSummary } from "../lib/openScienceGeneralSessionBridge";
import type { OpenScienceProjectSummary } from "../lib/openScienceProjectBridge";
import {
  GpuSessionButton,
  useDevGpuSessionControl,
  type DevGpuSessionControl
} from "./GpuSessionButton";

const DEV_GPU_SESSION_CONTROL_ENABLED =
  import.meta.env.DEV &&
  import.meta.env.VITE_DEV_GPU_SESSION_CONTROL === "true";

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
  const activeGroupTitle =
    moduleGroups.find((group) => group.items.some((item) => item.isActive))?.title ?? null;
  const [expandedGroupTitles, setExpandedGroupTitles] = useState<Set<string>>(() =>
    activeGroupTitle ? new Set([activeGroupTitle]) : new Set()
  );

  useEffect(() => {
    if (!activeGroupTitle) {
      return;
    }

    setExpandedGroupTitles((current) => {
      if (current.has(activeGroupTitle)) {
        return current;
      }

      const next = new Set(current);
      next.add(activeGroupTitle);
      return next;
    });
  }, [activeModule, activeGroupTitle]);

  function handleNavigate(action: () => void) {
    action();
    setIsMobileMenuOpen(false);
  }

  function handleToggleGroup(title: string) {
    setExpandedGroupTitles((current) => {
      const next = new Set(current);
      if (next.has(title)) {
        next.delete(title);
      } else {
        next.add(title);
      }
      return next;
    });
  }

  return (
    <div className="flex h-screen overflow-hidden bg-[#f4f6f8] text-slate-950">
      <aside className="hidden w-[276px] shrink-0 border-r border-slate-200/80 bg-[#f7f8fa] px-2.5 py-2 lg:block">
        <SidebarContent
          standaloneModules={standaloneModules}
          moduleGroups={moduleGroups}
          onOpenHome={() => handleNavigate(onOpenHome)}
          onNavigate={handleNavigate}
          expandedGroupTitles={expandedGroupTitles}
          onToggleGroup={handleToggleGroup}
          projects={projects}
          activeProjectDirectory={activeProjectDirectory}
          isProjectBridgeReady={isProjectBridgeReady}
          onOpenProject={(directory) => handleNavigate(() => onOpenProject(directory))}
          onBrowseProjects={() => handleNavigate(onBrowseProjects)}
          onNewProject={() => handleNavigate(onNewProject)}
          onSetProjectFavorite={onSetProjectFavorite}
          onArchiveProject={onArchiveProject}
          isGeneralWorkspaceActive={isGeneralWorkspaceActive}
          generalSessions={generalSessions}
          activeGeneralSessionID={activeGeneralSessionID}
          isGeneralSessionBridgeReady={isGeneralSessionBridgeReady}
          onOpenGeneralWorkspace={() => handleNavigate(onOpenGeneralWorkspace)}
          onNewGeneralSession={() => handleNavigate(onNewGeneralSession)}
          onOpenGeneralSession={(sessionID) => handleNavigate(() => onOpenGeneralSession(sessionID))}
          onRenameGeneralSession={onRenameGeneralSession}
          onDeleteGeneralSession={onDeleteGeneralSession}
          gpuSessionControl={DEV_GPU_SESSION_CONTROL_ENABLED ? gpuSessionControl : null}
          gpuStatusId="gpu-session-status-desktop"
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
              standaloneModules={standaloneModules}
              moduleGroups={moduleGroups}
              onOpenHome={() => handleNavigate(onOpenHome)}
              onNavigate={handleNavigate}
              expandedGroupTitles={expandedGroupTitles}
              onToggleGroup={handleToggleGroup}
              projects={projects}
              activeProjectDirectory={activeProjectDirectory}
              isProjectBridgeReady={isProjectBridgeReady}
              onOpenProject={(directory) => handleNavigate(() => onOpenProject(directory))}
              onBrowseProjects={() => handleNavigate(onBrowseProjects)}
              onNewProject={() => handleNavigate(onNewProject)}
              onSetProjectFavorite={onSetProjectFavorite}
              onArchiveProject={onArchiveProject}
              isGeneralWorkspaceActive={isGeneralWorkspaceActive}
              generalSessions={generalSessions}
              activeGeneralSessionID={activeGeneralSessionID}
              isGeneralSessionBridgeReady={isGeneralSessionBridgeReady}
              onOpenGeneralWorkspace={() => handleNavigate(onOpenGeneralWorkspace)}
              onNewGeneralSession={() => handleNavigate(onNewGeneralSession)}
              onOpenGeneralSession={(sessionID) => handleNavigate(() => onOpenGeneralSession(sessionID))}
              onRenameGeneralSession={onRenameGeneralSession}
              onDeleteGeneralSession={onDeleteGeneralSession}
              gpuSessionControl={DEV_GPU_SESSION_CONTROL_ENABLED ? gpuSessionControl : null}
              gpuStatusId="gpu-session-status-mobile"
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

        <main className={isHome ? "min-h-0 flex-1 overflow-hidden" : isResearchWorkbench ? `min-h-0 flex-1 overflow-hidden ${isReverseDesignWorkbench || isConditionalGenerationWorkbench || isStructureWorkbench || isDatabaseFilterWorkbench || isDatabaseAnalysisWorkbench || isKnowledgeWorkbench || isPolytaoWorkbench ? "p-0" : "py-5 md:py-8"}` : "flex-1 overflow-y-auto px-4 py-5 md:px-8 md:py-8"}>
          <div className={isHome ? "h-full" : ["relative mx-auto flex flex-col", isResearchWorkbench ? "h-full gap-0" : "gap-8", fullBleed ? "max-w-none" : "max-w-[1480px]"].join(" ")}>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}

type SidebarContentProps = {
  standaloneModules: AppShellModuleItem[];
  moduleGroups: AppShellModuleGroup[];
  onOpenHome: () => void;
  onNavigate: (action: () => void) => void;
  expandedGroupTitles: ReadonlySet<string>;
  onToggleGroup: (title: string) => void;
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
  gpuSessionControl: DevGpuSessionControl | null;
  gpuStatusId: string;
  trailing?: ReactNode;
};

function SidebarContent({
  standaloneModules,
  moduleGroups,
  onOpenHome,
  onNavigate,
  expandedGroupTitles,
  onToggleGroup,
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
  gpuSessionControl,
  gpuStatusId,
  trailing
}: SidebarContentProps) {
  const [isProjectExpanded, setIsProjectExpanded] = useState(false);
  const [generalSessionQuery, setGeneralSessionQuery] = useState("");
  const normalizedQuery = generalSessionQuery.trim().toLocaleLowerCase();
  const filteredGeneralSessions = normalizedQuery
    ? generalSessions.filter((session) =>
        (session.title.trim() || "未命名会话").toLocaleLowerCase().includes(normalizedQuery)
      )
    : generalSessions;
  const activeProject = activeProjectDirectory
    ? projects.find((project) => project.directory === activeProjectDirectory) ?? null
    : null;

  useEffect(() => {
    if (isGeneralWorkspaceActive) {
      setIsProjectExpanded(false);
    }
  }, [isGeneralWorkspaceActive]);

  return (
    <div className="flex h-full min-h-0 flex-col gap-1.5">
      <div className="flex shrink-0 items-center justify-between px-1.5 py-1">
        <button type="button" className="flex min-w-0 items-center gap-2 text-left" onClick={onOpenHome}>
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-slate-950 text-white shadow-sm">
            <MessageSquare className="h-4 w-4" />
          </span>
          <span className="block min-w-0 truncate text-sm font-semibold text-slate-950">智聚万物</span>
        </button>
        {gpuSessionControl || trailing ? (
          <div className="flex items-center gap-1">
            {gpuSessionControl ? (
              <GpuSessionButton control={gpuSessionControl} statusId={gpuStatusId} />
            ) : null}
            {trailing}
          </div>
        ) : null}
      </div>

      <div className="project-list-scrollbar min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-0.5">
        <nav aria-label="业务模块">
          <div className="flex flex-col gap-1 pb-2 pt-1">
            {standaloneModules.length > 0 ? (
              <div className="space-y-0.5 border-b border-slate-200/80 pb-1.5">
                {standaloneModules.map((item) => (
                  <SidebarModuleButton key={item.id} item={item} onNavigate={onNavigate} />
                ))}
              </div>
            ) : null}

            {moduleGroups.map((group) => {
              const hasItems = group.items.length > 0;
              const isExpanded = hasItems && expandedGroupTitles.has(group.title);

              return (
                <section key={group.title} className="space-y-0.5">
                  <button
                    type="button"
                    aria-label={group.title}
                    aria-expanded={isExpanded}
                    disabled={!hasItems}
                    className={[
                      "flex w-full items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left text-xs font-semibold tracking-[0.04em] transition-colors",
                      isExpanded
                        ? "bg-slate-200/70 text-slate-800"
                        : "text-slate-600 hover:bg-slate-200/60 hover:text-slate-900",
                      "disabled:cursor-default disabled:text-slate-500 disabled:hover:bg-transparent disabled:hover:text-slate-500"
                    ].join(" ")}
                    onClick={() => onToggleGroup(group.title)}
                  >
                    <span className="truncate">{group.title}</span>
                    {hasItems ? (
                      <ChevronDown
                        aria-hidden="true"
                        className={[
                          "h-3.5 w-3.5 shrink-0 transition-transform",
                          isExpanded ? "rotate-0" : "-rotate-90"
                        ].join(" ")}
                      />
                    ) : (
                      <span
                        aria-hidden="true"
                        className="shrink-0 text-[10px] font-medium text-slate-400"
                      >
                        暂无模块
                      </span>
                    )}
                  </button>

                  {isExpanded ? (
                    <div className="space-y-0.5 pl-1">
                      {group.items.map((item) => (
                        <SidebarModuleButton key={item.id} item={item} onNavigate={onNavigate} />
                      ))}
                    </div>
                  ) : null}
                </section>
              );
            })}
          </div>
        </nav>

        <section
          className="flex flex-col border-t border-slate-200/80 pt-2"
          aria-labelledby="project-list-title"
        >
          <div className="flex shrink-0 items-center gap-2 px-2 py-1.5">
            <button
              type="button"
              aria-label={isProjectExpanded ? "收起项目" : "展开项目"}
              aria-expanded={isProjectExpanded}
              className="flex min-w-0 items-center gap-2 rounded-md text-slate-700 transition-colors hover:text-slate-950"
              onClick={() => setIsProjectExpanded((current) => !current)}
            >
              <Folder className="h-3.5 w-3.5 text-slate-500" aria-hidden="true" />
              <h2 id="project-list-title" className="text-xs font-semibold uppercase tracking-[0.08em]">
                项目
              </h2>
              <ChevronDown
                aria-hidden="true"
                className={[
                  "h-3.5 w-3.5 shrink-0 text-slate-400 transition-transform",
                  isProjectExpanded ? "rotate-0" : "-rotate-90"
                ].join(" ")}
              />
            </button>
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

          {isProjectExpanded ? (
            <>
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

              <div>
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
            </>
          ) : activeProject && !isGeneralWorkspaceActive ? (
            <div className="pb-2">
              <ProjectListItem
                project={activeProject}
                active
                onOpen={() => onOpenProject(activeProject.directory)}
                onSetFavorite={(favorite) => onSetProjectFavorite(activeProject.directory, favorite)}
                onArchive={() => onArchiveProject(activeProject.directory)}
              />
            </div>
          ) : null}
        </section>

        <section
          className="flex flex-col border-t border-slate-200/80 pt-2"
          aria-labelledby="general-session-list-title"
        >
          <div className="flex shrink-0 items-center gap-2 px-2 py-1.5">
            <button
              type="button"
              aria-current={isGeneralWorkspaceActive ? "page" : undefined}
              className={[
                "flex min-w-0 items-center gap-2 rounded-md text-xs font-semibold uppercase tracking-[0.08em] transition-colors",
                isGeneralWorkspaceActive
                  ? "text-teal-700"
                  : "text-slate-700 hover:text-slate-950"
              ].join(" ")}
              onClick={() => {
                if (!isGeneralWorkspaceActive) {
                  onOpenGeneralWorkspace();
                }
              }}
            >
              <MessageSquare
                className={[
                  "h-3.5 w-3.5",
                  isGeneralWorkspaceActive ? "text-teal-600" : "text-slate-500"
                ].join(" ")}
                aria-hidden="true"
              />
              <h2 id="general-session-list-title">对话</h2>
            </button>
            {isGeneralWorkspaceActive ? (
              <div className="ml-auto flex items-center gap-1">
                {isGeneralSessionBridgeReady ? (
                  <span className="px-1 text-[11px] tabular-nums text-slate-400">{generalSessions.length}</span>
                ) : null}
                <button
                  type="button"
                  aria-label="新建对话"
                  title="新建对话"
                  disabled={!isGeneralSessionBridgeReady}
                  className="inline-flex h-6 w-6 items-center justify-center rounded-md text-slate-500 transition-colors hover:bg-white hover:text-teal-700 disabled:pointer-events-none disabled:opacity-35"
                  onClick={onNewGeneralSession}
                >
                  <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              </div>
            ) : null}
          </div>

          {isGeneralWorkspaceActive ? (
            <>
              <div className="relative shrink-0 px-1 pb-1">
                <Search className="pointer-events-none absolute left-3 top-2.5 h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
                <input
                  type="search"
                  aria-label="搜索会话"
                  placeholder="搜索会话"
                  value={generalSessionQuery}
                  className="h-8 w-full rounded-lg border border-slate-200 bg-white/70 pl-8 pr-7 text-[13px] text-slate-800 outline-none transition-colors placeholder:text-slate-400 focus:border-slate-300 focus:bg-white"
                  onChange={(event) => setGeneralSessionQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      setGeneralSessionQuery("");
                      event.currentTarget.blur();
                    }
                  }}
                />
                {generalSessionQuery ? (
                  <button
                    type="button"
                    aria-label="清除会话搜索"
                    className="absolute right-2.5 top-2 inline-flex h-5 w-5 items-center justify-center rounded text-slate-400 hover:bg-slate-100 hover:text-slate-700"
                    onClick={() => setGeneralSessionQuery("")}
                  >
                    <X className="h-3 w-3" aria-hidden="true" />
                  </button>
                ) : null}
              </div>

              <div>
                {!isGeneralSessionBridgeReady ? (
                  <p className="px-2 py-2 text-xs text-slate-400">正在同步会话…</p>
                ) : generalSessions.length === 0 ? (
                  <p className="px-2 py-2 text-xs leading-relaxed text-slate-400">暂无对话，点击右上角新建。</p>
                ) : filteredGeneralSessions.length === 0 ? (
                  <p className="px-2 py-2 text-xs text-slate-400">没有匹配的会话</p>
                ) : (
                  <div className="space-y-0.5 pb-2">
                    {filteredGeneralSessions.map((session) => (
                      <GeneralSessionListItem
                        key={session.id}
                        session={session}
                        active={session.id === activeGeneralSessionID}
                        onOpen={() => onOpenGeneralSession(session.id)}
                        onRename={(title) => onRenameGeneralSession(session.id, title)}
                        onDelete={() => onDeleteGeneralSession(session.id)}
                      />
                    ))}
                  </div>
                )}
              </div>
            </>
          ) : null}
        </section>
      </div>
    </div>
  );
}

function SidebarModuleButton({
  item,
  onNavigate
}: {
  item: AppShellModuleItem;
  onNavigate: (action: () => void) => void;
}) {
  return (
    <button
      type="button"
      data-module-id={item.id}
      aria-current={item.isActive ? "page" : undefined}
      className={[
        "group flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors",
        item.isActive
          ? "bg-teal-50/80 text-teal-950"
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
  );
}

function GeneralSessionListItem({
  session,
  active,
  onOpen,
  onRename,
  onDelete
}: {
  session: OpenScienceGeneralSessionSummary;
  active: boolean;
  onOpen: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
}) {
  const title = session.title.trim() || "未命名会话";
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useLayoutEffect(() => {
    if (!isEditing) {
      return;
    }
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [isEditing]);

  function startEditing() {
    setDraft(title);
    setIsEditing(true);
  }

  function cancelEditing() {
    setDraft(title);
    setIsEditing(false);
  }

  function commitEditing() {
    if (!isEditing) {
      return;
    }
    const nextTitle = draft.trim();
    setIsEditing(false);
    if (nextTitle && nextTitle !== title) {
      onRename(nextTitle);
    }
  }

  return (
    <div
      className={[
        "general-session-list-item group relative flex min-h-11 w-full items-stretch rounded-lg transition-colors",
        active
          ? "bg-white text-slate-950 shadow-sm ring-1 ring-slate-200"
          : "text-slate-600 hover:bg-white/78 hover:text-slate-950"
      ].join(" ")}
    >
      {isEditing ? (
        <div className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5">
          <span
            className={[
              "h-2 w-2 shrink-0 rounded-full",
              active ? "bg-teal-600" : "bg-slate-300"
            ].join(" ")}
            aria-hidden="true"
          />
          <input
            ref={inputRef}
            type="text"
            aria-label="重命名会话"
            value={draft}
            className="min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-1.5 py-1 text-[13px] text-slate-900 outline-none ring-2 ring-teal-500/15"
            onChange={(event) => setDraft(event.target.value)}
            onBlur={commitEditing}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                commitEditing();
              } else if (event.key === "Escape") {
                event.preventDefault();
                cancelEditing();
              }
            }}
          />
        </div>
      ) : (
        <button
          type="button"
          aria-current={active ? "page" : undefined}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-lg px-2 py-1.5 pr-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-teal-500/50"
          onClick={onOpen}
          onDoubleClick={(event) => {
            event.stopPropagation();
            startEditing();
          }}
        >
          <span
            className={[
              "h-2 w-2 shrink-0 rounded-full",
              active ? "bg-teal-600" : "bg-slate-300 group-hover:bg-slate-400"
            ].join(" ")}
            aria-hidden="true"
          />
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium" title="双击重命名">
            {title}
          </span>
        </button>
      )}
      {!isEditing ? (
        <button
          type="button"
          aria-label={`删除会话 ${title}`}
          title="删除会话"
          className="general-session-actions-trigger my-1 mr-1 inline-flex w-7 shrink-0 items-center justify-center rounded-md text-slate-400 transition-colors hover:bg-rose-50 hover:text-rose-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-400/40"
          onClick={() => {
            if (window.confirm(`确定删除会话“${title}”吗？此操作不可恢复。`)) {
              onDelete();
            }
          }}
        >
          <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      ) : null}
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
