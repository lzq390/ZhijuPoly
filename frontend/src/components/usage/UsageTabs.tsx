import { BarChart3, History, WalletCards } from "lucide-react";
import { useRef, type KeyboardEvent } from "react";

export type UsageTab = "overview" | "operations" | "resources";
const tabs = [
  { id: "overview", label: "次数总览", description: "当前周期与分类余额", icon: WalletCards },
  { id: "operations", label: "操作记录", description: "回执与已有结果", icon: History },
  { id: "resources", label: "资源观测", description: "任务与资源诊断", icon: BarChart3 },
] as const;

export function UsageTabs({ value, onChange }: { value: UsageTab; onChange: (value: UsageTab) => void }) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  function navigate(event: KeyboardEvent<HTMLDivElement>) {
    const current = tabs.findIndex(tab => tab.id === value);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
      : event.key === "ArrowRight" ? (current + 1) % tabs.length
      : event.key === "ArrowLeft" ? (current + tabs.length - 1) % tabs.length : null;
    if (next === null) return;
    event.preventDefault();
    onChange(tabs[next].id);
    refs.current[next]?.focus();
  }
  return <div className="np-usage-tabs" role="tablist" aria-label="用量视图" onKeyDown={navigate}>
    {tabs.map(({ id, label, description, icon: Icon }, index) => <button key={id} ref={element => { refs.current[index] = element; }}
      type="button" role="tab" id={`usage-tab-${id}`} aria-controls={`usage-panel-${id}`} aria-selected={value === id}
      aria-label={label} tabIndex={value === id ? 0 : -1} className={`np-usage-tab${value === id ? " is-active" : ""}`} onClick={() => onChange(id)}>
      <Icon aria-hidden="true" /><span><strong>{label}</strong><small>{description}</small></span>
    </button>)}
  </div>;
}
