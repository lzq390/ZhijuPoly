import { useRef, useState } from "react";
import { BarChart3, RefreshCw } from "lucide-react";
import { useAuth } from "../auth/AuthProvider";
import { requestServiceAccess } from "../auth/guestAccess";
import { ModulePageHeader } from "./ModulePageHeader";
import { PersonalEntitlementsPanel } from "./usage/PersonalEntitlementsPanel";
import { UsageResourcesPanel } from "./usage/UsageResourcesPanel";
import { UsageTabs, type UsageTab } from "./usage/UsageTabs";
import "./PersonalUsagePage.css";

export function PersonalUsagePage() {
  const auth = useAuth();
  if (auth?.status !== "authenticated") return <section className="np-module-page np-usage-page">
    <ModulePageHeader>我的用量</ModulePageHeader>
    <div className="np-module-page-body np-usage-body"><div className="np-usage-content"><div className="np-usage-panel np-usage-empty">
      <BarChart3 aria-hidden="true" /><h2>登录后查看我的用量</h2><p hidden>这里仅展示当前账号自己的统计记录。</p>
      <button type="button" onClick={() => auth ? auth.login() : requestServiceAccess()}>登录账号</button>
    </div></div></div>
  </section>;
  // Session identity owns every filter/result. privateFetch also fences in-flight bodies.
  return <SignedInUsage key={`${auth.session.user!.id}:${auth.session.session_id}`} />;
}

function SignedInUsage() {
  const [tab, setTab] = useState<UsageTab>("overview");
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [entitlementsBusy, setEntitlementsBusy] = useState(true);
  const [resourcesBusy, setResourcesBusy] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const busy = entitlementsBusy || resourcesBusy;

  function changeTab(next: UsageTab) {
    setTab(next);
    scrollRef.current?.scrollTo?.({ top: 0 });
  }
  return <section className="np-module-page np-usage-page">
    <ModulePageHeader actions={<button type="button" className="np-usage-refresh" onClick={() => setRefreshVersion(value => value + 1)} disabled={busy} aria-label="刷新全部">
      <RefreshCw aria-hidden="true" className={busy ? "is-refreshing" : undefined} /><span>{busy ? "更新中" : "刷新全部"}</span>
    </button>}>我的用量</ModulePageHeader>
    <div ref={scrollRef} className="np-module-page-body np-usage-body"><div className="np-usage-content">
      <UsageTabs value={tab} onChange={changeTab} />
      <PersonalEntitlementsPanel activeTab={tab} refreshVersion={refreshVersion} onBusyChange={setEntitlementsBusy} />
      <div className="np-usage-tabpanel" id="usage-panel-resources" role="tabpanel" aria-labelledby="usage-tab-resources" hidden={tab !== "resources"}>
        <UsageResourcesPanel refreshVersion={refreshVersion} onBusyChange={setResourcesBusy} />
      </div>
    </div></div>
  </section>;
}
