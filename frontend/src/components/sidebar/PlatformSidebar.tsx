import type { KeyboardEvent, ReactNode, RefObject } from "react";
import { useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Archive,
  ChevronDown,
  Ellipsis,
  Folder,
  MessageSquare,
  Plus,
  Search,
  Star,
  Trash2,
  X
} from "lucide-react";
import type { OpenScienceGeneralSessionSummary } from "../../lib/openScienceGeneralSessionBridge";
import type { OpenScienceProjectSummary } from "../../lib/openScienceProjectBridge";
import {
  GpuSessionButton,
  type DevGpuSessionControl
} from "../GpuSessionButton";

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
  id: "discover" | "build" | "optimize" | "data";
  label: string;
  secondaryLabel: string;
  items: AppShellModuleItem[];
  emptyLabel?: string;
};

type PlatformSidebarProps = {
  standaloneModules: AppShellModuleItem[];
  moduleGroups: AppShellModuleGroup[];
  onOpenHome: () => void;
  onNavigate: (action: () => void) => void;
  expandedGroupIds: ReadonlySet<AppShellModuleGroup["id"]>;
  onToggleGroup: (groupId: AppShellModuleGroup["id"]) => void;
  projects: OpenScienceProjectSummary[];
  activeProjectDirectory: string | null;
  isProjectBridgeReady: boolean;
  onOpenProject: (directory: string) => void;
  onBrowseProjects: () => void;
  onNewProject: () => void;
  onSetProjectFavorite: (directory: string, favorite: boolean) => void;
  onArchiveProject: (directory: string) => void;
  isProjectExpanded: boolean;
  onProjectExpandedChange: (expanded: boolean) => void;
  isGeneralWorkspaceActive: boolean;
  generalSessions: OpenScienceGeneralSessionSummary[];
  activeGeneralSessionID: string | null;
  isGeneralSessionBridgeReady: boolean;
  onOpenGeneralWorkspace: () => void;
  onNewGeneralSession: () => void;
  onOpenGeneralSession: (sessionID: string) => void;
  onRenameGeneralSession: (sessionID: string, title: string) => void;
  onDeleteGeneralSession: (sessionID: string) => void;
  generalSessionQuery: string;
  onGeneralSessionQueryChange: (query: string) => void;
  gpuSessionControl: DevGpuSessionControl | null;
  gpuStatusId: string;
  closeButtonRef?: RefObject<HTMLButtonElement | null>;
  onClose?: () => void;
};

export function PlatformSidebar({
  standaloneModules,
  moduleGroups,
  onOpenHome,
  onNavigate,
  expandedGroupIds,
  onToggleGroup,
  projects,
  activeProjectDirectory,
  isProjectBridgeReady,
  onOpenProject,
  onBrowseProjects,
  onNewProject,
  onSetProjectFavorite,
  onArchiveProject,
  isProjectExpanded,
  onProjectExpandedChange,
  isGeneralWorkspaceActive,
  generalSessions,
  activeGeneralSessionID,
  isGeneralSessionBridgeReady,
  onOpenGeneralWorkspace,
  onNewGeneralSession,
  onOpenGeneralSession,
  onRenameGeneralSession,
  onDeleteGeneralSession,
  generalSessionQuery,
  onGeneralSessionQueryChange,
  gpuSessionControl,
  gpuStatusId,
  closeButtonRef,
  onClose
}: PlatformSidebarProps) {
  const normalizedQuery = generalSessionQuery.trim().toLocaleLowerCase();
  const filteredGeneralSessions = normalizedQuery
    ? generalSessions.filter((session) =>
        (session.title.trim() || "未命名会话").toLocaleLowerCase().includes(normalizedQuery)
      )
    : generalSessions;
  const activeProject = activeProjectDirectory
    ? projects.find((project) => project.directory === activeProjectDirectory) ?? null
    : null;

  return (
    <div className="np-sidebar">
      <SidebarBrand
        onOpenHome={onOpenHome}
        gpuSessionControl={gpuSessionControl}
        gpuStatusId={gpuStatusId}
        closeButtonRef={closeButtonRef}
        onClose={onClose}
      />

      <div className="np-sidebar__scroll" data-sidebar-scroll-region>
        <SidebarModuleNavigation
          standaloneModules={standaloneModules}
          moduleGroups={moduleGroups}
          expandedGroupIds={expandedGroupIds}
          onToggleGroup={onToggleGroup}
          onNavigate={onNavigate}
        />

        <SidebarProjectSection
          projects={projects}
          activeProjectDirectory={activeProjectDirectory}
          activeProject={activeProject}
          isProjectBridgeReady={isProjectBridgeReady}
          isProjectExpanded={isProjectExpanded}
          onProjectExpandedChange={onProjectExpandedChange}
          isGeneralWorkspaceActive={isGeneralWorkspaceActive}
          onOpenProject={onOpenProject}
          onBrowseProjects={onBrowseProjects}
          onNewProject={onNewProject}
          onSetProjectFavorite={onSetProjectFavorite}
          onArchiveProject={onArchiveProject}
        />

        <SidebarSessionSection
          isGeneralWorkspaceActive={isGeneralWorkspaceActive}
          generalSessions={generalSessions}
          filteredGeneralSessions={filteredGeneralSessions}
          activeGeneralSessionID={activeGeneralSessionID}
          isGeneralSessionBridgeReady={isGeneralSessionBridgeReady}
          onOpenGeneralWorkspace={onOpenGeneralWorkspace}
          onNewGeneralSession={onNewGeneralSession}
          onOpenGeneralSession={onOpenGeneralSession}
          onRenameGeneralSession={onRenameGeneralSession}
          onDeleteGeneralSession={onDeleteGeneralSession}
          generalSessionQuery={generalSessionQuery}
          onGeneralSessionQueryChange={onGeneralSessionQueryChange}
        />
      </div>
    </div>
  );
}

function SidebarBrand({
  onOpenHome,
  gpuSessionControl,
  gpuStatusId,
  closeButtonRef,
  onClose
}: {
  onOpenHome: () => void;
  gpuSessionControl: DevGpuSessionControl | null;
  gpuStatusId: string;
  closeButtonRef?: RefObject<HTMLButtonElement | null>;
  onClose?: () => void;
}) {
  return (
    <div className="np-sidebar__brand">
      <button type="button" className="np-sidebar__brand-link" onClick={onOpenHome}>
        <span className="np-sidebar__brand-mark" aria-hidden="true">
          <MessageSquare />
        </span>
        <span className="np-sidebar__brand-copy">
          <span className="np-sidebar__brand-name">智聚万物</span>
          <span className="np-sidebar__brand-subtitle" aria-hidden="true">NexPoly Lab</span>
        </span>
      </button>
      {gpuSessionControl || onClose ? (
        <div className="np-sidebar__brand-actions">
          {gpuSessionControl ? (
            <GpuSessionButton control={gpuSessionControl} statusId={gpuStatusId} />
          ) : null}
          {onClose ? (
            <button
              ref={closeButtonRef}
              type="button"
              aria-label="关闭导航"
              className="np-sidebar__icon-button np-sidebar__close-button"
              onClick={onClose}
            >
              <X aria-hidden="true" />
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function SidebarModuleNavigation({
  standaloneModules,
  moduleGroups,
  expandedGroupIds,
  onToggleGroup,
  onNavigate
}: {
  standaloneModules: AppShellModuleItem[];
  moduleGroups: AppShellModuleGroup[];
  expandedGroupIds: ReadonlySet<AppShellModuleGroup["id"]>;
  onToggleGroup: (groupId: AppShellModuleGroup["id"]) => void;
  onNavigate: (action: () => void) => void;
}) {
  return (
    <nav className="np-sidebar__modules" aria-label="业务模块">
      {standaloneModules.length > 0 ? (
        <div className="np-sidebar__standalone">
          {standaloneModules.map((item) => (
            <SidebarModuleButton key={item.id} item={item} onNavigate={onNavigate} />
          ))}
        </div>
      ) : null}

      <div className="np-sidebar__groups">
        {moduleGroups.map((group) => {
          const hasItems = group.items.length > 0;
          const isExpanded = hasItems && expandedGroupIds.has(group.id);
          const accessibleLabel = `${group.label} ${group.secondaryLabel}`;

          return (
            <section key={group.id} className="np-sidebar-group" data-group-id={group.id}>
              <button
                type="button"
                aria-label={accessibleLabel}
                aria-expanded={isExpanded}
                disabled={!hasItems}
                className="np-sidebar-group__trigger"
                data-expanded={isExpanded ? "true" : "false"}
                onClick={() => onToggleGroup(group.id)}
              >
                <span className="np-sidebar-group__labels" aria-hidden="true">
                  <span className="np-sidebar-group__label">{group.label}</span>
                  <span className="np-sidebar-group__secondary">{group.secondaryLabel}</span>
                </span>
                {hasItems ? (
                  <ChevronDown className="np-sidebar-group__chevron" aria-hidden="true" />
                ) : (
                  <span className="np-sidebar-group__empty">
                    {group.emptyLabel ?? "暂无模块"}
                  </span>
                )}
              </button>

              {isExpanded ? (
                <div className="np-sidebar-group__items">
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
      data-active={item.isActive ? "true" : "false"}
      aria-current={item.isActive ? "page" : undefined}
      title={item.description}
      className="np-sidebar-module"
      onClick={() => onNavigate(item.onClick)}
    >
      <span className="np-sidebar-module__icon" aria-hidden="true">
        {item.icon}
      </span>
      <span className="np-sidebar-module__label">{item.label}</span>
    </button>
  );
}

function SidebarProjectSection({
  projects,
  activeProjectDirectory,
  activeProject,
  isProjectBridgeReady,
  isProjectExpanded,
  onProjectExpandedChange,
  isGeneralWorkspaceActive,
  onOpenProject,
  onBrowseProjects,
  onNewProject,
  onSetProjectFavorite,
  onArchiveProject
}: {
  projects: OpenScienceProjectSummary[];
  activeProjectDirectory: string | null;
  activeProject: OpenScienceProjectSummary | null;
  isProjectBridgeReady: boolean;
  isProjectExpanded: boolean;
  onProjectExpandedChange: (expanded: boolean) => void;
  isGeneralWorkspaceActive: boolean;
  onOpenProject: (directory: string) => void;
  onBrowseProjects: () => void;
  onNewProject: () => void;
  onSetProjectFavorite: (directory: string, favorite: boolean) => void;
  onArchiveProject: (directory: string) => void;
}) {
  const headingId = useId();

  return (
    <section className="np-sidebar-section" aria-labelledby={headingId}>
      <div className="np-sidebar-section__header">
        <button
          type="button"
          aria-label={isProjectExpanded ? "收起项目" : "展开项目"}
          aria-expanded={isProjectExpanded}
          className="np-sidebar-section__title-button"
          onClick={() => onProjectExpandedChange(!isProjectExpanded)}
        >
          <Folder aria-hidden="true" />
          <h2 id={headingId}>项目</h2>
          <ChevronDown
            className="np-sidebar-section__chevron"
            data-expanded={isProjectExpanded ? "true" : "false"}
            aria-hidden="true"
          />
        </button>
        <div className="np-sidebar-section__actions">
          {isProjectBridgeReady ? (
            <span className="np-sidebar-section__count">{projects.length}</span>
          ) : null}
          <button
            type="button"
            aria-label="新建项目"
            title="新建项目"
            disabled={!isProjectBridgeReady}
            className="np-sidebar__icon-button np-sidebar__icon-button--small"
            onClick={onNewProject}
          >
            <Plus aria-hidden="true" />
          </button>
        </div>
      </div>

      {isProjectExpanded ? (
        <div className="np-sidebar-section__content">
          <button
            type="button"
            disabled={!isProjectBridgeReady}
            className="np-sidebar-search-action"
            onClick={onBrowseProjects}
          >
            <Search aria-hidden="true" />
            <span>搜索项目</span>
          </button>

          {!isProjectBridgeReady ? (
            <p className="np-sidebar-empty">正在同步项目…</p>
          ) : projects.length === 0 ? (
            <p className="np-sidebar-empty">暂无项目</p>
          ) : (
            <div className="np-sidebar-list">
              {projects.map((project) => (
                <ProjectListItem
                  key={project.directory}
                  project={project}
                  active={project.directory === activeProjectDirectory}
                  onOpen={() => onOpenProject(project.directory)}
                  onSetFavorite={(favorite) =>
                    onSetProjectFavorite(project.directory, favorite)
                  }
                  onArchive={() => onArchiveProject(project.directory)}
                />
              ))}
            </div>
          )}
        </div>
      ) : activeProject && !isGeneralWorkspaceActive ? (
        <div className="np-sidebar-section__summary">
          <ProjectListItem
            project={activeProject}
            active
            onOpen={() => onOpenProject(activeProject.directory)}
            onSetFavorite={(favorite) =>
              onSetProjectFavorite(activeProject.directory, favorite)
            }
            onArchive={() => onArchiveProject(activeProject.directory)}
          />
        </div>
      ) : null}
    </section>
  );
}

function SidebarSessionSection({
  isGeneralWorkspaceActive,
  generalSessions,
  filteredGeneralSessions,
  activeGeneralSessionID,
  isGeneralSessionBridgeReady,
  onOpenGeneralWorkspace,
  onNewGeneralSession,
  onOpenGeneralSession,
  onRenameGeneralSession,
  onDeleteGeneralSession,
  generalSessionQuery,
  onGeneralSessionQueryChange
}: {
  isGeneralWorkspaceActive: boolean;
  generalSessions: OpenScienceGeneralSessionSummary[];
  filteredGeneralSessions: OpenScienceGeneralSessionSummary[];
  activeGeneralSessionID: string | null;
  isGeneralSessionBridgeReady: boolean;
  onOpenGeneralWorkspace: () => void;
  onNewGeneralSession: () => void;
  onOpenGeneralSession: (sessionID: string) => void;
  onRenameGeneralSession: (sessionID: string, title: string) => void;
  onDeleteGeneralSession: (sessionID: string) => void;
  generalSessionQuery: string;
  onGeneralSessionQueryChange: (query: string) => void;
}) {
  const headingId = useId();

  return (
    <section className="np-sidebar-section" aria-labelledby={headingId}>
      <div
        className="np-sidebar-section__header"
        data-workspace-active={isGeneralWorkspaceActive ? "true" : "false"}
      >
        <button
          type="button"
          aria-current={isGeneralWorkspaceActive ? "page" : undefined}
          data-active={isGeneralWorkspaceActive ? "true" : "false"}
          className="np-sidebar-section__title-button np-sidebar-section__title-button--workspace"
          onClick={() => {
            if (!isGeneralWorkspaceActive) {
              onOpenGeneralWorkspace();
            }
          }}
        >
          <MessageSquare aria-hidden="true" />
          <h2 id={headingId}>对话</h2>
        </button>
        {isGeneralWorkspaceActive ? (
          <div className="np-sidebar-section__actions">
            {isGeneralSessionBridgeReady ? (
              <span className="np-sidebar-section__count">{generalSessions.length}</span>
            ) : null}
            <button
              type="button"
              aria-label="新建对话"
              title="新建对话"
              disabled={!isGeneralSessionBridgeReady}
              className="np-sidebar__icon-button np-sidebar__icon-button--small"
              onClick={onNewGeneralSession}
            >
              <Plus aria-hidden="true" />
            </button>
          </div>
        ) : null}
      </div>

      {isGeneralWorkspaceActive ? (
        <div className="np-sidebar-section__content">
          <div className="np-sidebar-search">
            <Search aria-hidden="true" />
            <input
              type="search"
              aria-label="搜索会话"
              placeholder="搜索会话"
              value={generalSessionQuery}
              onChange={(event) => onGeneralSessionQueryChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  onGeneralSessionQueryChange("");
                  event.currentTarget.blur();
                }
              }}
            />
            {generalSessionQuery ? (
              <button
                type="button"
                aria-label="清除会话搜索"
                onClick={() => onGeneralSessionQueryChange("")}
              >
                <X aria-hidden="true" />
              </button>
            ) : null}
          </div>

          {!isGeneralSessionBridgeReady ? (
            <p className="np-sidebar-empty">正在同步会话…</p>
          ) : generalSessions.length === 0 ? (
            <p className="np-sidebar-empty np-sidebar-empty--multiline">
              暂无对话，点击右上角新建。
            </p>
          ) : filteredGeneralSessions.length === 0 ? (
            <p className="np-sidebar-empty">没有匹配的会话</p>
          ) : (
            <div className="np-sidebar-list">
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
      ) : null}
    </section>
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
    if (isEditing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
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
    <div className="np-sidebar-list-item np-sidebar-session" data-active={active ? "true" : "false"}>
      {isEditing ? (
        <div className="np-sidebar-session__editor">
          <span className="np-sidebar-session__status" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            aria-label="重命名会话"
            value={draft}
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
          className="np-sidebar-list-item__main np-sidebar-session__main"
          onClick={onOpen}
          onDoubleClick={(event) => {
            event.stopPropagation();
            startEditing();
          }}
        >
          <span className="np-sidebar-session__status" aria-hidden="true" />
          <span className="np-sidebar-list-item__title" title={title}>
            {title}
          </span>
        </button>
      )}
      {!isEditing ? (
        <button
          type="button"
          aria-label={`删除会话 ${title}`}
          title="删除会话"
          className="np-sidebar-list-item__action np-sidebar-session__action"
          onClick={() => {
            if (window.confirm(`确定删除会话“${title}”吗？此操作不可恢复。`)) {
              onDelete();
            }
          }}
        >
          <Trash2 aria-hidden="true" />
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
  const [menuPosition, setMenuPosition] = useState({
    top: PROJECT_MENU_MARGIN,
    left: PROJECT_MENU_MARGIN
  });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  function positionMenu() {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) {
      return;
    }
    const maxLeft = Math.max(
      PROJECT_MENU_MARGIN,
      window.innerWidth - PROJECT_MENU_WIDTH - PROJECT_MENU_MARGIN
    );
    const left = Math.min(
      Math.max(PROJECT_MENU_MARGIN, rect.right - PROJECT_MENU_WIDTH),
      maxLeft
    );
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
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenu(true);
      }
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
    if (
      event.key !== "ArrowDown" &&
      event.key !== "ArrowUp" &&
      event.key !== "Home" &&
      event.key !== "End"
    ) {
      return;
    }
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []
    );
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
          className="np-sidebar-project-menu"
          style={{ top: menuPosition.top, left: menuPosition.left }}
          onKeyDown={handleMenuKeyDown}
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              closeMenu(true);
              onSetFavorite(!project.favorite);
            }}
          >
            <Star
              className="np-sidebar-project-menu__star"
              data-favorite={project.favorite ? "true" : "false"}
              aria-hidden="true"
            />
            {project.favorite ? "取消收藏" : "收藏项目"}
          </button>
          <button
            type="button"
            role="menuitem"
            className="np-sidebar-project-menu__danger"
            onClick={() => {
              closeMenu();
              onArchive();
            }}
          >
            <Archive aria-hidden="true" />
            归档项目
          </button>
        </div>,
        document.body
      )
    : null;

  return (
    <div className="np-sidebar-list-item np-sidebar-project" data-active={active ? "true" : "false"}>
      <button
        type="button"
        data-project-directory={project.directory}
        aria-current={active ? "page" : undefined}
        title={project.directory}
        className="np-sidebar-list-item__main np-sidebar-project__main"
        onClick={onOpen}
      >
        <Folder className="np-sidebar-project__icon" aria-hidden="true" />
        <span className="np-sidebar-project__copy">
          <span className="np-sidebar-project__name-row">
            <span className="np-sidebar-list-item__title">{project.name}</span>
            {project.favorite ? (
              <Star className="np-sidebar-project__favorite" aria-hidden="true" />
            ) : null}
          </span>
          <span className="np-sidebar-project__path">{project.displayPath}</span>
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
        className="np-sidebar-list-item__action np-sidebar-project__action"
        onClick={() => setMenuOpen((current) => !current)}
      >
        <Ellipsis aria-hidden="true" />
      </button>
      {menu}
    </div>
  );
}
