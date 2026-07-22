import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { ChevronDown, Folder, Menu, MessageSquare, Star, X } from "lucide-react";
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
          {isProjectBridgeReady ? (
            <span className="ml-auto text-[11px] tabular-nums text-slate-400">{projects.length}</span>
          ) : null}
        </div>

        <div className="project-list-scrollbar min-h-0 flex-1 overflow-y-auto pr-0.5">
          {!isProjectBridgeReady ? (
            <p className="px-2 py-2 text-xs text-slate-400">正在同步项目…</p>
          ) : projects.length === 0 ? (
            <p className="px-2 py-2 text-xs text-slate-400">暂无项目</p>
          ) : (
            <div className="space-y-0.5 pb-2">
              {projects.map((project) => {
                const isActive = project.directory === activeProjectDirectory;
                return (
                  <button
                    key={project.directory}
                    type="button"
                    data-project-directory={project.directory}
                    aria-current={isActive ? "page" : undefined}
                    title={project.directory}
                    className={[
                      "group flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors",
                      isActive
                        ? "bg-white text-slate-950 shadow-sm ring-1 ring-slate-200"
                        : "text-slate-600 hover:bg-white/78 hover:text-slate-950"
                    ].join(" ")}
                    onClick={() => onOpenProject(project.directory)}
                  >
                    <Folder
                      className={[
                        "h-4 w-4 shrink-0",
                        isActive ? "text-teal-700" : "text-slate-400 group-hover:text-teal-700"
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
                );
              })}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
