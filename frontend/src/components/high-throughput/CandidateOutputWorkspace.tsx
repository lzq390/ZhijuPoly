import { ArrowLeft, BadgeInfo, CheckCircle2, ChevronRight, Circle, FileCheck2, GitCompareArrows, Layers3, LoaderCircle, LockKeyhole, Square, Star, Target } from "lucide-react";
import { type CSSProperties, type ReactNode, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { HighThroughputTarget, HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";
import { cn } from "../../lib/utils";
import { AGENT_PROPERTY_ICONS, TargetTabs } from "./PriorWorkbench";
import { CandidateStructureDetails } from "./RecommendationValidation";
import { buildCandidateOutputSummary, candidateMeetsTarget, type CandidateOutputSummary } from "./candidate-output-model";
import { formatIterationValue as formatValue } from "./iteration-model";
import { displayTargetUnit } from "./prior-hotspot-model";
import type { RecommendationValidationValues } from "./types";
import "./candidate-output-workspace.css";

type Props = {
  targets: HighThroughputTarget[];
  candidateTotal: number;
  materialType: string;
  representation: string;
  activeTargetKey: HighThroughputTargetKey;
  onSelectTarget: (key: HighThroughputTargetKey) => void;
  validationValues: RecommendationValidationValues;
  onBack: () => void;
  onNext: () => void;
  canAdvance: boolean;
  transitionMessage: string | null;
  renderCandidateMap: (summary: CandidateOutputSummary, selectedId: string, onSelect: (id: string) => void) => ReactNode;
};

export function CandidateOutputWorkspace({ targets, candidateTotal, materialType, representation, activeTargetKey,
  onSelectTarget, validationValues, onBack, onNext, canAdvance, transitionMessage, renderCandidateMap }: Props) {
  const id = useId();
  const footerRef = useRef<HTMLElement>(null);
  const detailRef = useRef<HTMLDivElement>(null);
  const [previewIds, setPreviewIds] = useState<Partial<Record<HighThroughputTargetKey, string>>>({});
  const summaries = useMemo(() => targets.map((target) => buildCandidateOutputSummary(target, validationValues)), [targets, validationValues]);
  const active = summaries.find((summary) => summary.target.key === activeTargetKey) ?? summaries[0];
  const { target, component, output, candidates, recordBest } = active;
  const selected = candidates.find((candidate) => candidate.id === previewIds[target.key]) ?? output;
  const isOutput = selected.id === output.id;
  const isBusy = Boolean(transitionMessage);
  const PropertyIcon = AGENT_PROPERTY_ICONS[target.key];

  useLayoutEffect(() => {
    const footer = footerRef.current;
    const scroll = footer?.closest<HTMLElement>(".ht-scroll-region");
    if (!footer || !scroll) return;
    const stageNav = scroll.querySelector<HTMLElement>(".ht-flow-steps");
    const previousStageScroll = stageNav?.scrollLeft ?? 0;
    const sync = () => {
      scroll.style.setProperty("--ht-s4-footer-height", `${footer.offsetHeight}px`);
      // On phones the seven-stage strip scrolls horizontally. Reveal S4
      // without moving the document, the main scroll region, or focus.
      const current = stageNav?.querySelector<HTMLElement>('[aria-current="step"]');
      if (!stageNav || !current || stageNav.scrollWidth <= stageNav.clientWidth) return;
      const navBounds = stageNav.getBoundingClientRect();
      const currentBounds = current.getBoundingClientRect();
      if (currentBounds.left < navBounds.left || currentBounds.right > navBounds.right) {
        stageNav.scrollLeft += currentBounds.left - navBounds.left - (navBounds.width - currentBounds.width) / 2;
      }
    };
    sync();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(sync);
    observer?.observe(footer);
    if (stageNav) observer?.observe(stageNav);
    return () => { observer?.disconnect(); scroll.style.removeProperty("--ht-s4-footer-height"); if (stageNav) stageNav.scrollLeft = previousStageScroll; };
  }, []);

  function selectCandidate(candidateId: string, fromMap = false) {
    if (isBusy || !candidates.some((candidate) => candidate.id === candidateId)) return;
    setPreviewIds((current) => ({ ...current, [target.key]: candidateId }));
    if (!fromMap) return;
    const detail = detailRef.current;
    const scroll = detail?.closest<HTMLElement>(".ht-scroll-region");
    if (!detail || !scroll) return;
    const bounds = detail.getBoundingClientRect();
    const region = scroll.getBoundingClientRect();
    const bottom = Math.min(region.bottom, footerRef.current?.getBoundingClientRect().top ?? region.bottom);
    if (bounds.top < region.top + 12 || bounds.bottom > bottom - 12) {
      scroll.scrollTo({ top: scroll.scrollTop + bounds.top - region.top - 12,
        behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }
  }

  return <div className="ht-s1-stage ht-s4-stage" aria-busy={isBusy}>
    <div className="ht-s1-content">
      <div className="ht-workbench-demo-note"><BadgeInfo aria-hidden="true" size={16} />
        <span><strong>演示说明：</strong>当前展示预设输出 p1–p4 及模拟性质值。S3 验证值仅用于回流记录对照，不替换输出、备选或后续配方路径；查看备选仅用于比较。</span>
      </div>
      <div className="ht-s1-summary">
        <div className="ht-s1-scenario-facts"><Layers3 aria-hidden="true" /><span>{materialType}</span>
          <span><b>{candidateTotal.toLocaleString("en-US")}</b> 候选</span><span>{representation} · 2D</span></div>
        <div className="ht-s4-summary-status"><LockKeyhole aria-hidden="true" /><span>预设组分 <b>{summaries.length} / {summaries.length}</b> · 待配比搜索</span></div>
      </div>

      <section className="ht-s1-preview ht-s4-output-overview" aria-labelledby={`${id}-outputs-heading`}>
        <header className="ht-s1-preview-header"><div className="ht-s1-section-title"><span className="ht-s1-section-index">01</span><div>
          <h3 id={`${id}-outputs-heading`}>四个预设输出组分</h3><p>四个 Agent 的固定输出；选择组分查看性质与候选来源</p>
        </div></div><span className="ht-s4-section-caption"><FileCheck2 aria-hidden="true" />S3 → S4 → S5</span></header>
        <div className="ht-s4-output-tabs">
          <TargetTabs targets={targets} activeTargetKey={target.key} onSelectTarget={onSelectTarget} disabled={isBusy}
            tabId={id} panelId={`${id}-detail`} label="选择预设输出组分"
            renderIcon={(item) => { const Icon = AGENT_PROPERTY_ICONS[item.key]; return <Icon aria-hidden="true" />; }}
            getTabLabel={(item) => `${summaries.find((summary) => summary.target.key === item.key)!.component.id} ${item.shortLabel} 预设输出`}
            renderContent={(item) => {
              const summary = summaries.find((entry) => entry.target.key === item.key)!;
              const Icon = AGENT_PROPERTY_ICONS[item.key];
              return <>
                <span className="ht-s4-output-card-title"><b>{summary.component.id}</b><strong>{item.shortLabel}</strong><Icon aria-hidden="true" /></span>
                <span className="ht-s4-output-card-id">{summary.output.id}</span>
                <span className="ht-s4-output-card-description">{summary.component.description}</span>
                <span className="ht-s4-output-card-value"><b>{formatValue(item, summary.output.scores[item.key])}</b><small>{displayTargetUnit(item)} · 预测/模拟</small></span>
                <span className={cn("ht-s4-attainment", summary.meetsTarget && "met")}>{summary.meetsTarget ? <CheckCircle2 aria-hidden="true" /> : <Target aria-hidden="true" />}
                  {summary.meetsTarget ? "已达场景阈值" : "未达场景阈值"}</span>
              </>;
            }} />
        </div>
      </section>

      <section className="ht-s1-preview ht-validation-workspace ht-s4-detail" id={`${id}-detail`} role="tabpanel"
        aria-labelledby={`${id}-${target.key}`} tabIndex={0} style={{ "--target-color": target.color } as CSSProperties}>
        <header className="ht-s1-preview-header"><div className="ht-s1-section-title"><span className="ht-s1-section-index">02</span><div>
          <h3>候选性质与来源</h3><p>固定输出与备选独立展示，结构展开状态在切换时保留</p>
        </div></div><span className="ht-s4-section-caption"><PropertyIcon aria-hidden="true" /><b>{component.id}</b> · {target.shortLabel} Agent</span></header>
        <div className="ht-s4-detail-layout">
          <div className="ht-s4-detail-main">
            <div className="ht-s4-candidate-focus" ref={detailRef}>
              <div className="ht-s4-candidate-heading" role="status" aria-live="polite" aria-atomic="true">
                <span>{isOutput ? "预设输出候选" : "备选预览 · 不替换输出"}</span><strong>{selected.id}</strong>
              </div>
              <div className="ht-s4-candidate-facts"><span>单体组合 <b>{selected.monomerA} + {selected.monomerB}</b></span><span>结构类别 <span>{selected.cluster}</span></span></div>
              <div className="ht-s4-property-grid" aria-label={`${selected.id} 四性质预测与阈值`}>
                {targets.map((item) => {
                  const meets = candidateMeetsTarget(selected, item);
                  const Icon = AGENT_PROPERTY_ICONS[item.key];
                  return <article key={item.key} className="ht-s4-property" data-property={item.key} style={{ "--property-color": item.color } as CSSProperties}>
                    <div className="ht-s4-property-label"><Icon aria-hidden="true" /><strong>{item.shortLabel}</strong><span>预测/模拟</span></div>
                    <p className="ht-s4-property-value"><b>{formatValue(item, selected.scores[item.key])}</b><span>{displayTargetUnit(item)}</span></p>
                    <div className="ht-s4-property-threshold">目标 {item.direction === "lower" ? "≤" : "≥"} <b>{formatValue(item, item.target)}</b> {displayTargetUnit(item)}</div>
                    <span className={cn("ht-s4-attainment", meets && "met")}>{meets ? <CheckCircle2 aria-hidden="true" /> : <Circle aria-hidden="true" />}{meets ? "已达到场景阈值" : "尚未达到场景阈值"}</span>
                  </article>;
                })}
              </div>
            </div>
            <div className="ht-s4-provenance">
              <div><LockKeyhole aria-hidden="true" /><span>固定组分 <b>{component.id}</b></span><strong>{output.id}</strong><small>{component.description} · 来自预设收敛路径</small></div>
              <div><GitCompareArrows aria-hidden="true" /><span>S3 回流记录最优</span><strong>{recordBest?.candidate.id ?? "—"}</strong>
                <span><b>{recordBest ? formatValue(target, recordBest.value) : "—"}</b> {displayTargetUnit(target)}</span><small>{recordBest?.source} · 仅作证据对照</small></div>
              <p>DOE <b>{active.doeCount}</b> + 已回流 <b>{active.returnedCount}</b>；{target.shortLabel} {target.direction === "lower" ? "越低越好" : "越高越好"}。记录最优与预设输出分别保留，不互相替换。</p>
            </div>
          </div>

          <aside className="ht-s4-position-panel ht-s1-space-panel" aria-labelledby={`${id}-position-heading`}>
            <header className="ht-s1-space-header"><Layers3 className="ht-s1-space-watermark" aria-hidden="true" />
              <div className="ht-s1-section-title"><div><h4 id={`${id}-position-heading`}>候选位置与备选</h4><p>仅作投影定位，不代表性能排名</p></div></div></header>
            <div className="ht-s1-space-body">
              <div className="ht-s1-map ht-s4-position-map">{renderCandidateMap(active, selected.id, (candidateId) => selectCandidate(candidateId, true))}</div>
              <div className="ht-s1-map-legend"><span><i className="candidate" aria-hidden="true" />候选点</span><span><Star aria-hidden="true" />固定输出</span><span><Square aria-hidden="true" />备选</span><span className="ht-s4-map-note">{representation} 投影 X / Y · 热点为预设区域示意</span></div>
              <div className="ht-s4-candidate-list" aria-label={`${target.shortLabel} 输出与备选列表`}>
                {candidates.map((candidate, index) => <button type="button" key={candidate.id} onClick={() => selectCandidate(candidate.id, true)} disabled={isBusy}
                  aria-label={`查看 ${candidate.id} ${index === 0 ? "固定输出" : "备选"}`} aria-pressed={selected.id === candidate.id}>
                  {index === 0 ? <Star aria-hidden="true" /> : <Square aria-hidden="true" />}
                  <span><strong>{candidate.id}</strong><small>{index === 0 ? `${component.id} 固定输出` : `备选 ${index} · 仅比较`}</small></span>
                  <span className="ht-s4-list-value"><b>{formatValue(target, candidate.scores[target.key])}</b><small>{displayTargetUnit(target)}</small></span>
                </button>)}
              </div>
            </div>
          </aside>
          <div className="ht-s4-structure-slot"><CandidateStructureDetails candidate={selected}
            sourceLabel={isOutput ? `S4 ${component.id} · 预设收敛输出` : `S4 ${target.shortLabel} Top-k 备选 · 仅作比较`} disabled={isBusy} /></div>
        </div>
      </section>
    </div>
    <footer ref={footerRef} className="ht-s1-footer ht-s4-footer">
      <button type="button" className="ht-s1-secondary-button" onClick={onBack} disabled={isBusy}><ArrowLeft aria-hidden="true" />返回 S3 收敛对照</button>
      <p className="ht-s4-handoff-status" role="status" aria-live="polite">{isBusy ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : <LockKeyhole aria-hidden="true" />}<span>{transitionMessage || "p1–p4 固定组分已就绪 · 查看备选不改变配方池"}</span></p>
      <button type="button" className="ht-s1-primary-button" onClick={onNext} disabled={!canAdvance || isBusy}>
        {isBusy ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : null}{isBusy ? "正在进入配比搜索" : "进入 S5 多目标配比搜索"}{!isBusy ? <ChevronRight aria-hidden="true" /> : null}</button>
    </footer>
  </div>;
}
