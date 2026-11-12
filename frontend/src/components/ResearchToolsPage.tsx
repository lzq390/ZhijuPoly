import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { ChevronDown, MessagesSquare } from "lucide-react";
import type { AppShellModuleGroup } from "./sidebar/PlatformSidebar";
import { ResearchToolThumbnail } from "./ResearchToolThumbnail";
import "./research-tools.css";

export type ResearchToolsPageProps = {
  groups: AppShellModuleGroup[];
  expandedGroupIds: ReadonlySet<AppShellModuleGroup["id"]>;
  collapsedGroupIds: ReadonlySet<AppShellModuleGroup["id"]>;
  onToggleGroup: (id: AppShellModuleGroup["id"]) => void;
  onToggleCollapsedGroup: (id: AppShellModuleGroup["id"]) => void;
};

const toolDescriptions: Record<string, string> = {
  knowledge: "检索文献与合成知识，定位研究线索。",
  polytaoGeneration: "设定性质条件，生成候选重复单元。",
  monomerRetrosynthesis: "从目标单体反推单步前体组合。",
  explorer: "寻找结构或性能相近的聚合物。",
  databaseQuery: "通过结构或 SMILES 查询材料记录。",
  databaseFilter: "组合多项性质阈值，筛选目标材料。",
  database: "关联结构、过程与性能，探索材料数据。",
  homopolymerPrediction: "预测热学、力学与气体渗透性质。",
  monomerPolymerization: "从单体出发，构建聚合物结构。",
  mdSimulationDemo: "构建聚合物体系，开展分子动力学模拟。",
  monomerMdSimulation: "开展单体及多组分体系动力学模拟。",
  monomerDft: "计算分子构型、能量与振动性质。",
  conditionalGeneration: "围绕目标条件，生成聚合物候选结构。",
  reverseDesign: "围绕目标玻璃化转变温度，反推候选结构。",
  highThroughputWorkflowDemo: "探索候选推荐、单目标与配方混合优化。"
};

function columnsForWidth(width: number) {
  return width >= 1280 ? 4 : width >= 900 ? 3 : width >= 560 ? 2 : 1;
}

export function ResearchToolsPage({ groups, expandedGroupIds, collapsedGroupIds, onToggleGroup, onToggleCollapsedGroup }: ResearchToolsPageProps) {
  const contentRef = useRef<HTMLDivElement>(null);
  const groupIdPrefix = useId();
  const [columns, setColumns] = useState(() =>
    columnsForWidth(typeof window === "undefined" ? 0 : window.innerWidth)
  );
  const [activePreviewId, setActivePreviewId] = useState<string | null>(null);
  const [loadedPreviewId, setLoadedPreviewId] = useState<string | null>(null);
  const activePreviewRef = useRef(activePreviewId);
  const hoveredPreviewRef = useRef<string | null>(null);
  const focusedPreviewRef = useRef<string | null>(null);
  const failedPreviewsRef = useRef(new Set<string>());
  const previewRequestRef = useRef(0);
  const touchFocusRef = useRef(false);
  activePreviewRef.current = activePreviewId;

  const showPreview = (id: string | null) => {
    const next = id && !failedPreviewsRef.current.has(id) ? id : null;
    if (activePreviewRef.current === next) return;
    activePreviewRef.current = next;
    previewRequestRef.current += 1;
    setActivePreviewId(next);
    setLoadedPreviewId(null);
  };

  const clearPreview = () => {
    hoveredPreviewRef.current = null;
    focusedPreviewRef.current = null;
    failedPreviewsRef.current.clear();
    showPreview(null);
  };

  useLayoutEffect(() => {
    const visibleIds = new Set(groups.flatMap((group) => (
      collapsedGroupIds.has(group.id) ? [] : expandedGroupIds.has(group.id) ? group.items : group.items.slice(0, columns)
    ).map((item) => item.id)));
    if (hoveredPreviewRef.current && !visibleIds.has(hoveredPreviewRef.current)) hoveredPreviewRef.current = null;
    if (focusedPreviewRef.current && !visibleIds.has(focusedPreviewRef.current)) focusedPreviewRef.current = null;
    if (activePreviewId && !visibleIds.has(activePreviewId)) clearPreview();
  }, [activePreviewId, collapsedGroupIds, columns, expandedGroupIds, groups]);

  useEffect(() => {
    const stopWhenHidden = () => {
      if (document.hidden) clearPreview();
    };
    const allowKeyboardFocus = () => {
      touchFocusRef.current = false;
    };
    document.addEventListener("visibilitychange", stopWhenHidden);
    document.addEventListener("keydown", allowKeyboardFocus, true);
    return () => {
      activePreviewRef.current = null;
      previewRequestRef.current += 1;
      document.removeEventListener("visibilitychange", stopWhenHidden);
      document.removeEventListener("keydown", allowKeyboardFocus, true);
    };
  }, []);

  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content) return;

    const measure = () => {
      const width = content.getBoundingClientRect().width || content.clientWidth || window.innerWidth;
      setColumns(columnsForWidth(width));
    };
    measure();

    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver((entries) => {
        const entry = entries.find((candidate) => candidate.target === content);
        if (entry && entry.contentRect.width > 0) setColumns(columnsForWidth(entry.contentRect.width));
      });
      observer.observe(content);
      return () => observer.disconnect();
    }

    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);

  return (
    <div className="np-research-tools np-workbench-theme">
      <div
        className="np-research-tools__content"
        ref={contentRef}
        data-research-columns={columns}
        style={{ "--research-columns": columns } as CSSProperties}
      >
        <header className="np-research-tools__intro">
          <div>
            <h1>实验工具</h1>
            <p className="np-research-tools__lead">选择工具，开始材料发现、设计与实验优化。</p>
          </div>
          <svg className="np-research-tools__intro-art" viewBox="0 0 120 80" aria-hidden="true" focusable="false">
            <g fill="none" stroke="currentColor" strokeWidth="0.9">
              <path d="m23 25 26-15 26 15v30L49 70 23 55Z M49 10v30l26 15M23 25l26 15-26 15M49 40v30" />
              <path d="m75 25 24-14v30L75 55M99 11 73-4 49 10" opacity=".45" />
            </g>
            <g fill="currentColor">
              <circle cx="23" cy="25" r="2.8" /><circle cx="49" cy="40" r="4" />
              <circle cx="75" cy="55" r="2.8" /><circle cx="49" cy="10" r="2.8" />
            </g>
          </svg>
        </header>

        <div className="np-research-tools__groups">
          {groups.map((group, groupIndex) => {
            const expanded = expandedGroupIds.has(group.id);
            const collapsed = collapsedGroupIds.has(group.id);
            const remaining = Math.max(0, group.items.length - columns);
            const visibleItems = collapsed ? [] : expanded ? group.items : group.items.slice(0, columns);
            const headingId = `${groupIdPrefix}-${group.id}-heading`;
            const gridId = `${groupIdPrefix}-${group.id}-grid`;

            return (
              <section className="np-research-tools__group" aria-labelledby={headingId} key={group.id} data-research-group={group.id} data-research-collapsed={collapsed}>
                <div className="np-research-tools__group-header">
                  <div className="np-research-tools__group-heading">
                    <span className="np-research-tools__category-index" aria-hidden="true">{String(groupIndex + 1).padStart(2, "0")}</span>
                    <h2 id={headingId}>{group.label}<span className="np-research-tools__count" aria-label={`${group.items.length} 个工具`}>{group.items.length}</span></h2>
                  </div>
                  <span className="np-research-tools__group-separator" aria-hidden="true" />
                  <div className="np-research-tools__group-controls">
                    {!collapsed && remaining > 0 ? (
                      <button
                        type="button"
                        className="np-research-tools__expand"
                        aria-expanded={expanded}
                        aria-controls={gridId}
                        aria-label={`${group.label}：${expanded ? "仅显示一行" : `展开更多（${remaining}）`}`}
                        onClick={() => onToggleGroup(group.id)}
                      >
                        <span>{expanded ? "仅显示一行" : `展开更多（${remaining}）`}</span>
                        <ChevronDown aria-hidden="true" />
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="np-research-tools__expand np-research-tools__collapse"
                      aria-expanded={!collapsed}
                      aria-controls={gridId}
                      aria-label={`${group.label}：${collapsed ? "展开" : "收起"}`}
                      onClick={() => onToggleCollapsedGroup(group.id)}
                    >
                      <span>{collapsed ? "展开" : "收起"}</span>
                      <ChevronDown aria-hidden="true" />
                    </button>
                  </div>
                </div>
                <ul className="np-research-tools__grid" id={gridId} hidden={collapsed}>
                  {visibleItems.map((item) => {
                    const active = activePreviewId === item.id;
                    const ready = active && loadedPreviewId === item.id;
                    const requestId = previewRequestRef.current;
                    return (
                      <li key={item.id}>
                        <article className={`np-research-tool${active ? " np-research-tool--previewing" : ""}`}>
                          <button
                            type="button"
                            className="np-research-tool__entry"
                            aria-label={item.label}
                            data-module-id={item.id}
                            onClick={item.onClick}
                            onPointerDown={(event) => {
                              touchFocusRef.current = event.pointerType === "touch";
                            }}
                            onPointerEnter={(event) => {
                              if (event.pointerType === "touch" || hoveredPreviewRef.current === item.id) return;
                              touchFocusRef.current = false;
                              hoveredPreviewRef.current = item.id;
                              failedPreviewsRef.current.delete(item.id);
                              showPreview(item.id);
                            }}
                            onPointerLeave={(event) => {
                              if (event.pointerType === "touch") return;
                              const wasHovered = hoveredPreviewRef.current === item.id;
                              if (wasHovered) hoveredPreviewRef.current = null;
                              if (wasHovered || activePreviewRef.current === item.id) showPreview(focusedPreviewRef.current);
                            }}
                            onFocus={(event) => {
                              if (touchFocusRef.current || event.currentTarget.contains(event.relatedTarget) || focusedPreviewRef.current === item.id) return;
                              focusedPreviewRef.current = item.id;
                              failedPreviewsRef.current.delete(item.id);
                              showPreview(item.id);
                            }}
                            onBlur={(event) => {
                              if (event.currentTarget.contains(event.relatedTarget)) return;
                              const wasFocused = focusedPreviewRef.current === item.id;
                              if (wasFocused) focusedPreviewRef.current = null;
                              if (wasFocused || activePreviewRef.current === item.id) showPreview(hoveredPreviewRef.current);
                            }}
                          >
                            <span className="np-research-tool__thumbnail" aria-hidden="true">
                              <span className="np-research-tool__static" hidden={ready}>
                                <ResearchToolThumbnail moduleId={item.id} />
                              </span>
                              {active ? <img
                                className="np-research-tool__animation"
                                src={`/previews/research-tools/${item.id}.gif`}
                                alt=""
                                width={360}
                                height={180}
                                decoding="async"
                                hidden={!ready}
                                onLoad={() => {
                                  if (activePreviewRef.current === item.id && previewRequestRef.current === requestId) setLoadedPreviewId(item.id);
                                }}
                                onError={() => {
                                  if (activePreviewRef.current !== item.id || previewRequestRef.current !== requestId) return;
                                  failedPreviewsRef.current.add(item.id);
                                  showPreview(null);
                                }}
                              /> : null}
                            </span>
                            <span className="np-research-tool__copy">
                              <span className="np-research-tool__title">{item.label}</span>
                              <span className="np-research-tool__description">{toolDescriptions[item.id] ?? item.description}</span>
                            </span>
                          </button>
                        </article>
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })}
        </div>

        <p className="np-research-tools__footnote"><MessagesSquare aria-hidden="true" />需要梳理研究思路？从左侧「智能助手」开始。</p>
      </div>
    </div>
  );
}
