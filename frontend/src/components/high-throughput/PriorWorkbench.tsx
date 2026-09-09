import { ArrowDownRight, ArrowUpRight, Bot, ChevronLeft, ChevronRight, Gauge, MoveHorizontal, Shrink, Thermometer } from "lucide-react";
import { type CSSProperties, type KeyboardEvent, type ReactNode, type RefObject, useId, useLayoutEffect, useRef, useState } from "react";
import type { HighThroughputTarget, HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";
import { cn } from "../../lib/utils";
import "./prior-import-workspace.css";

// Retain the established S1 class names so both stages share identical geometry and theme.
// Narrow canvases show one floating card at a time to keep its controls reachable.
const COMPACT_AGENT_LAYOUT_WIDTH = 836;
const INSET_AGENT_CLEARANCE = 12;
const AGENT_SIDES: HighThroughputTargetKey[][] = [["tg", "cte"], ["elongation", "modulus"]];
type OpenAgents = Partial<Record<HighThroughputTargetKey, boolean>>;
type AgentLayoutMode = "outside" | "inset" | "compact";

function agentLayoutMode(layout: HTMLElement): AgentLayoutMode {
  if (layout.clientWidth > 0 && layout.clientWidth < COMPACT_AGENT_LAYOUT_WIDTH) return "compact";
  // CSS owns the container/native-2K breakpoints; interaction follows the same mode.
  return getComputedStyle(layout).getPropertyValue("--ht-s1-inset-agents").trim() === "1" ? "inset" : "outside";
}

function fitOpenAgents(agents: OpenAgents, mode: AgentLayoutMode, preferred: HighThroughputTargetKey): OpenAgents {
  if (mode === "outside") return agents;
  const groups = mode === "compact" ? [AGENT_SIDES.flat()] : AGENT_SIDES;
  let next = agents;
  for (const group of groups) {
    const open = group.filter((key) => agents[key]);
    if (open.length <= 1) continue;
    const keep = open.includes(preferred) ? preferred : open[open.length - 1];
    if (next === agents) next = { ...agents };
    for (const key of open) next[key] = key === keep;
  }
  return next;
}

export const AGENT_PROPERTY_ICONS = {
  tg: Thermometer,
  cte: Shrink,
  elongation: MoveHorizontal,
  modulus: Gauge,
} satisfies Record<HighThroughputTargetKey, typeof Thermometer>;


export type AgentCardProps = {
  selectId: string;
  index: number;
  target: HighThroughputTarget;
  selected: boolean;
  onSelect: () => void;
  disabled: boolean;
};

export function AgentDockLayout({ targets, activeTargetKey, onSelectTarget, disabled, mapRef, renderAgent, children }: {
  targets: HighThroughputTarget[];
  activeTargetKey: HighThroughputTargetKey;
  onSelectTarget: (key: HighThroughputTargetKey) => void;
  disabled: boolean;
  mapRef: RefObject<HTMLDivElement | null>;
  renderAgent: (props: AgentCardProps) => ReactNode;
  children: ReactNode;
}) {
  const [openAgents, setOpenAgents] = useState<OpenAgents>({});
  const layoutRef = useRef<HTMLDivElement>(null);
  const lastOpenTarget = useRef<HighThroughputTargetKey>("tg");
  function toggleAgent(targetKey: HighThroughputTargetKey) {
    const willOpen = !openAgents[targetKey];
    const mode = layoutRef.current ? agentLayoutMode(layoutRef.current) : "outside";
    if (willOpen) {
      lastOpenTarget.current = targetKey;
      onSelectTarget(targetKey);
    }
    setOpenAgents((agents) => fitOpenAgents({ ...agents, [targetKey]: willOpen }, mode, targetKey));
  }

  useLayoutEffect(() => {
    const layout = layoutRef.current;
    const map = mapRef.current;
    if (!layout || !map) return;
    const syncLayout = () => {
      const plot = map.getBoundingClientRect();
      if (plot.height > 0) {
        layout.style.setProperty("--ht-s1-inset-height", `${Math.max(0, plot.height - 2 * INSET_AGENT_CLEARANCE)}px`);
        layout.querySelectorAll<HTMLElement>(".ht-s1-agent-disclosure").forEach((agent) => {
          const upper = agent.style.getPropertyValue("--agent-row") === "1";
          const anchor = upper ? plot.top + INSET_AGENT_CLEARANCE : plot.bottom - INSET_AGENT_CLEARANCE;
          agent.style.setProperty("--ht-s1-inset-anchor", `${anchor - agent.getBoundingClientRect().top}px`);
        });
      }
      const focusedAgent = document.activeElement?.closest<HTMLElement>("[data-agent-theme]")?.dataset.agentTheme as HighThroughputTargetKey | undefined;
      setOpenAgents((agents) => fitOpenAgents(agents, agentLayoutMode(layout), focusedAgent ?? lastOpenTarget.current));
    };
    syncLayout();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(syncLayout);
    observer.observe(layout);
    observer.observe(map);
    // The board can stay 1560px wide while outside clearance changes on resize.
    const workbench = layout.closest(".ht-workbench-page");
    if (workbench) observer.observe(workbench);
    return () => observer.disconnect();
  }, []);


  return (
    <div ref={layoutRef} className="ht-s1-agent-layout">
      {targets.map((target, index) => (
        <AgentDisclosure key={target.key} target={target} index={index} open={Boolean(openAgents[target.key])}
          onToggle={() => toggleAgent(target.key)} selected={activeTargetKey === target.key}
          onSelect={() => onSelectTarget(target.key)} disabled={disabled} renderAgent={renderAgent} />
      ))}
      {children}
    </div>
  );
}

function AgentDisclosure({ target, index, open, onToggle, selected, onSelect, disabled, renderAgent }: Omit<AgentCardProps, "selectId"> & {
  open: boolean;
  onToggle: () => void;
  renderAgent: (props: AgentCardProps) => ReactNode;
}) {
  const panelId = useId();
  const toggleRef = useRef<HTMLButtonElement>(null);
  const buttonLabel = `${open ? "收起" : "展开"} ${target.shortLabel} Agent`;
  const ToggleIcon = index < 2
    ? (open ? ChevronRight : ChevronLeft)
    : (open ? ChevronLeft : ChevronRight);
  return (
    <aside className={cn("ht-s1-agent-disclosure", index < 2 ? "left" : "right", open && "expanded", selected && "selected")}
      aria-label={`${target.shortLabel} Agent 面板`} data-agent-theme={target.key}
      style={{ "--target-color": target.color, "--agent-column": index < 2 ? 1 : 3, "--agent-row": index % 2 + 1 } as CSSProperties}>
      <button ref={toggleRef} type="button" className="ht-s1-agent-toggle" aria-expanded={open} aria-controls={panelId}
        aria-label={buttonLabel} title={buttonLabel} disabled={disabled && !open}
        onClick={() => { onToggle(); toggleRef.current?.focus({ preventScroll: true }); }}>
        <ToggleIcon aria-hidden="true" />
      </button>
      <div className="ht-s1-agent-flyout" id={panelId} hidden={!open}>
        {renderAgent({ selectId: `${panelId}-select`, index, target, selected, onSelect, disabled })}
      </div>
    </aside>
  );
}

export function AgentCardShell({ selectId, index, target, selected, onSelect, disabled, stageLabel, targetValueLabel, children }: AgentCardProps & {
  stageLabel: string;
  targetValueLabel?: string;
  children: ReactNode;
}) {
  const selectRef = useRef<HTMLButtonElement>(null);
  const DirectionIcon = target.direction === "higher" ? ArrowUpRight : ArrowDownRight;
  const PropertyIcon = AGENT_PROPERTY_ICONS[target.key];
  const unitLabel = target.unit === "degC" ? "°C" : target.unit;
  return (
    <article
      className={cn("ht-s1-agent-card", selected && "selected")}
      style={{ "--target-color": target.color } as CSSProperties}
      data-disabled={disabled}
      onClick={(event) => {
        // Only the outer card selects; inset data panels and controls keep their own behavior.
        if (disabled || (event.target as Element).closest(".ht-s1-agent-target, .ht-s1-upload-area, [data-agent-inset], button, input, a, select, textarea, [role='button']")) return;
        onSelect();
        selectRef.current?.focus({ preventScroll: true });
      }}
    >
      <header className="ht-s1-agent-header">
        <PropertyIcon className="ht-s1-agent-watermark" aria-hidden="true" />
        <div className="ht-s1-agent-meta">
          <span className="ht-s1-agent-id">AGENT <b>{String(index + 1).padStart(2, "0")}</b></span>
          <span className="ht-s1-agent-view-label">{selected ? "当前查看" : stageLabel}</span>
        </div>
        <button ref={selectRef} type="button" id={selectId} className="ht-s1-agent-select" onClick={onSelect} aria-pressed={selected} aria-label={`查看 ${target.shortLabel} Agent`} disabled={disabled}>
          <span className="ht-s1-agent-icon"><Bot aria-hidden="true" /></span>
          <span><strong>{target.shortLabel} Agent</strong><small>{target.label}</small></span>
          <ChevronRight aria-hidden="true" />
        </button>
      </header>
      <div className="ht-s1-agent-body">
        <div className="ht-s1-agent-target">
          <div className="ht-s1-agent-target-label">
            <span>目标阈值</span>
            <small><DirectionIcon aria-hidden="true" />{target.direction === "higher" ? "越高越好" : "越低越好"}</small>
          </div>
          <strong><span className="ht-s1-target-operator">{target.direction === "higher" ? "≥" : "≤"}</span> {targetValueLabel ?? (target.key === "modulus" ? target.target.toFixed(1) : target.target)} <small>{unitLabel}</small></strong>
        </div>
        {children}
      </div>
    </article>
  );
}

export function TargetTabs({ targets, activeTargetKey, onSelectTarget, disabled, tabId, panelId, label, renderIcon }: {
  targets: HighThroughputTarget[];
  activeTargetKey: HighThroughputTargetKey;
  onSelectTarget: (key: HighThroughputTargetKey) => void;
  disabled: boolean;
  tabId: string;
  panelId: string;
  label: string;
  renderIcon: (target: HighThroughputTarget) => ReactNode;
}) {
  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (disabled) return;
    let nextIndex: number;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % targets.length;
    else if (event.key === "ArrowLeft") nextIndex = (index + targets.length - 1) % targets.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = targets.length - 1;
    else return;
    event.preventDefault();
    onSelectTarget(targets[nextIndex].key);
    document.getElementById(`${tabId}-${targets[nextIndex].key}`)?.focus();
  }

  return (
    <div className="ht-s1-target-tabs" role="tablist" aria-label={label}>
      {targets.map((target, index) => (
        <button
          key={target.key}
          id={`${tabId}-${target.key}`}
          type="button"
          role="tab"
          aria-selected={target.key === activeTargetKey}
          aria-controls={panelId}
          tabIndex={target.key === activeTargetKey ? 0 : -1}
          onClick={() => onSelectTarget(target.key)}
          onKeyDown={(event) => handleTabKeyDown(event, index)}
          disabled={disabled}
          style={{ "--target-color": target.color } as CSSProperties}
        >
          {renderIcon(target)}
          {target.shortLabel}
        </button>
      ))}
    </div>
  );
}
