import type { ReactNode } from "react";
import { useState } from "react";
import { Home, Menu, MessageSquare, X } from "lucide-react";
import { Button } from "./ui/button";

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
  children: ReactNode;
};

export function AppShell({ activeModule, fullBleed = false, moduleGroups, onOpenHome, children }: AppShellProps) {
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const isHome = activeModule === "home";
  const isExplorer = activeModule === "explorer";

  function handleNavigate(action: () => void) {
    action();
    setIsMobileMenuOpen(false);
  }

  return (
    <div className="flex h-screen overflow-hidden bg-[#f4f6f8] text-slate-950">
      <aside className="hidden w-[276px] shrink-0 border-r border-slate-200/80 bg-[#f7f8fa] px-3 py-3 lg:block">
        <SidebarContent
          isHome={isHome}
          moduleGroups={moduleGroups}
          onOpenHome={() => handleNavigate(onOpenHome)}
          onNavigate={handleNavigate}
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
          <aside className="relative h-full w-[86vw] max-w-[340px] border-r border-slate-200 bg-[#f7f8fa] px-3 py-3 shadow-2xl">
            <SidebarContent
              isHome={isHome}
              moduleGroups={moduleGroups}
              onOpenHome={() => handleNavigate(onOpenHome)}
              onNavigate={handleNavigate}
              trailing={
                <button
                  type="button"
                  aria-label="关闭导航"
                  className="inline-flex h-9 w-9 items-center justify-center rounded-xl text-slate-500 hover:bg-slate-200/70 hover:text-slate-950"
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

        <main className={isHome ? "min-h-0 flex-1 overflow-hidden" : isExplorer ? "min-h-0 flex-1 overflow-hidden py-5 md:py-8" : "flex-1 overflow-y-auto px-4 py-5 md:px-8 md:py-8"}>
          <div className={isHome ? "h-full" : ["relative mx-auto flex flex-col", isExplorer ? "h-full gap-0" : "gap-8", fullBleed ? "max-w-none" : "max-w-[1480px]"].join(" ")}>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}

type SidebarContentProps = {
  isHome: boolean;
  moduleGroups: AppShellModuleGroup[];
  onOpenHome: () => void;
  onNavigate: (action: () => void) => void;
  trailing?: ReactNode;
};

function SidebarContent({ isHome, moduleGroups, onOpenHome, onNavigate, trailing }: SidebarContentProps) {
  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between px-2 py-2">
        <button type="button" className="flex min-w-0 items-center gap-3 text-left" onClick={onOpenHome}>
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-slate-950 text-white shadow-sm">
            <MessageSquare className="h-5 w-5" />
          </span>
          <span className="block min-w-0 truncate text-sm font-semibold text-slate-950">智聚万物</span>
        </button>
        {trailing}
      </div>

      <Button
        type="button"
        variant={isHome ? "default" : "outline"}
        className="h-11 justify-start rounded-2xl px-3"
        onClick={onOpenHome}
      >
        <Home className="mr-2 h-4 w-4" />
        智聚万物
      </Button>

      <nav className="min-h-0 flex-1 overflow-y-auto pr-1">
        <div className="flex flex-col gap-5 pb-4 pt-2">
          {moduleGroups.map((group) => (
            <section key={group.title} className="space-y-1.5">
              <div className="px-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-400">
                {group.title}
              </div>
              <div className="space-y-1">
                {group.items.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={[
                      "group flex w-full items-center gap-3 rounded-2xl px-3 py-2.5 text-left transition",
                      item.isActive
                        ? "bg-white text-slate-950 shadow-sm ring-1 ring-slate-200"
                        : "text-slate-600 hover:bg-white/78 hover:text-slate-950"
                    ].join(" ")}
                    onClick={() => onNavigate(item.onClick)}
                  >
                    <span
                      className={[
                        "flex h-8 w-8 shrink-0 items-center justify-center rounded-xl border transition",
                        item.isActive
                          ? "border-teal-200 bg-teal-50 text-teal-700"
                          : "border-slate-200 bg-white/70 text-slate-500 group-hover:text-teal-700"
                      ].join(" ")}
                    >
                      {item.icon}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-sm font-medium">{item.label}</span>
                  </button>
                ))}
              </div>
            </section>
          ))}
        </div>
      </nav>
    </div>
  );
}
