import type { ReactNode } from "react";
import { ChevronRight, FileSpreadsheet, Grid2X2, LockKeyhole, MessageSquare } from "lucide-react";
import "../components/sidebar/platform-sidebar.css";

/** Guest navigation contains only local/static content; private providers never mount. */
export function GuestShell({ children, account, onLogin }: { children: ReactNode; account: ReactNode; onLogin: () => void }) {
  const scrollTo = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  return <div className="np-app-shell np-guest-shell">
    <aside className="np-guest-sidebar" aria-label="工作空间导航">
      <div className="np-sidebar">
        <div className="np-sidebar__brand">
          <button type="button" className="np-sidebar__brand-link" onClick={() => scrollTo("guest-introduction")}>
            <span className="np-sidebar__brand-mark" aria-hidden="true"><MessageSquare /></span>
            <span className="np-sidebar__brand-copy"><span className="np-sidebar__brand-name">智聚万物</span><span className="np-sidebar__brand-subtitle">NexPoly Lab</span></span>
          </button>
        </div>
        <div className="np-sidebar__scroll">
          <nav className="np-guest-navigation" aria-label="游客导航">
            <button type="button" className="np-guest-navigation__active" aria-label="结构工作台，本地体验" onClick={() => scrollTo("guest-local-workspace")}><Grid2X2 aria-hidden="true" /><span>结构工作台<small>本地体验</small></span></button>
            <div className="np-guest-navigation__groups">
              {[["材料发现", "DISCOVER"], ["材料设计", "BUILD"], ["实验优化", "OPTIMIZE"], ["数据管理", "DATA"]].map(([label, english]) =>
                <button key={english} type="button" onClick={onLogin} aria-label={`${label}，登录后使用`}><span>{label}<small>{english}</small></span><LockKeyhole className="np-guest-navigation__lock" aria-hidden="true" /></button>
              )}
            </div>
            <button type="button" aria-label="公开模板" onClick={() => scrollTo("guest-batch-templates")}><FileSpreadsheet aria-hidden="true" /><span>公开模板</span><ChevronRight aria-hidden="true" /></button>
          </nav>
          <p className="np-guest-sidebar__note">游客可体验本地画板与公开模板。登录后可查询、计算并管理自己的科研资产。</p>
        </div>
        {account}
      </div>
    </aside>
    <main className="np-guest-main">
      <header className="np-guest-main__header"><span>结构工作台</span><span className="np-guest-badge">游客体验</span></header>
      <div className="np-guest-main__content">{children}</div>
    </main>
  </div>;
}
