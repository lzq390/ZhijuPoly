import { ArrowLeft, BadgeInfo, CheckCircle2, Circle, FlaskConical, LockKeyhole, RotateCcw } from "lucide-react";
import { useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { highThroughputDemoScenario as scenario, type HighThroughputTarget, type HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";
import { cn } from "../../lib/utils";
import { AGENT_PROPERTY_ICONS, TargetTabs } from "./PriorWorkbench";
import { CandidateStructureDetails } from "./RecommendationValidation";
import { formatIterationValue as formatValue } from "./iteration-model";
import { displayTargetUnit } from "./prior-hotspot-model";
import { buildFinalFormulationSummary } from "./final-formulation-model";
import "./final-formulation-workspace.css";

type Props = {
  targets: HighThroughputTarget[];
  onBack: () => void;
  onRestart: () => void;
};

export function FinalFormulationWorkspace({ targets, onBack, onRestart }: Props) {
  const id = useId();
  const footerRef = useRef<HTMLElement>(null);
  const [activeTargetKey, setActiveTargetKey] = useState<HighThroughputTargetKey>("tg");
  const summary = useMemo(() => buildFinalFormulationSummary(targets), [targets]);
  const { mix, sources, outcomes, passedCount } = summary;
  const source = sources.find((item) => item.target.key === activeTargetKey) ?? sources[0];

  useLayoutEffect(() => {
    const footer = footerRef.current;
    const scroll = footer?.closest<HTMLElement>(".ht-scroll-region");
    if (!footer || !scroll) return;
    const nav = scroll.querySelector<HTMLElement>(".ht-flow-steps");
    const previousLeft = nav?.scrollLeft ?? 0;
    const sync = () => {
      scroll.style.setProperty("--ht-s6-footer-height", `${footer.offsetHeight}px`);
      const active = nav?.querySelector<HTMLElement>('[aria-current="step"]');
      if (!nav || !active || nav.scrollWidth <= nav.clientWidth) return;
      const bounds = nav.getBoundingClientRect(), item = active.getBoundingClientRect();
      if (item.left < bounds.left || item.right > bounds.right) nav.scrollLeft += item.left - bounds.left - (bounds.width - item.width) / 2;
    };
    sync();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(sync);
    observer?.observe(footer);
    if (nav) observer?.observe(nav);
    return () => { observer?.disconnect(); scroll.style.removeProperty("--ht-s6-footer-height"); if (nav) nav.scrollLeft = previousLeft; };
  }, []);

  return <div className="ht-s1-stage ht-s6-stage">
    <div className="ht-s1-content">
      <div className="ht-workbench-demo-note"><BadgeInfo aria-hidden="true" /><span><strong>演示说明：</strong>固定场景演示，结果为预设模拟数据；修改验证记录不改变最终配方，仍需真实实验验证。</span></div>

      <section className="ht-s1-preview" aria-labelledby={`${id}-formulation`}>
        <header className="ht-s1-preview-header"><div className="ht-s1-section-title"><span className="ht-s1-section-index">01</span><div><h3 id={`${id}-formulation`}>最终推荐配方</h3><p>四组分配比与推荐理由</p></div></div></header>
        <div className="ht-s6-final-layout">
          <div className="ht-s6-recipe">
            <div className="ht-s6-recipe-heading"><div className="ht-s6-recipe-id"><span>推荐配方</span><strong>{mix.id}</strong></div>
              <div className="ht-s6-recipe-metrics"><div className="ht-s6-score"><span>预设综合分</span><p><b>{mix.score}</b><span> / <b>100</b></span></p></div>
                <div className={cn("ht-s6-result-status", passedCount < outcomes.length && "partial")}><span>达到目标阈值</span><p>{passedCount === outcomes.length ? <CheckCircle2 aria-hidden="true" /> : <Circle aria-hidden="true" />}<b>{passedCount}/{outcomes.length}</b></p></div>
              </div>
            </div>
            <div className="ht-s6-composition" aria-label={`${mix.id} 最终组分比例`}>
              <div className="ht-s6-ratio-bar" aria-hidden="true">{sources.map(({ component, ratio }) => <span key={component.id} style={{ width: `${ratio * 100}%`, backgroundColor: component.color }} />)}</div>
              <dl>{sources.map(({ component, ratio }) => <div key={component.id} style={{ "--component-color": component.color } as CSSProperties}><dt><i />{component.id}</dt><dd><b>{Math.round(ratio * 100)}</b><span>%</span></dd></div>)}</dl>
            </div>
            <p className="ht-s6-recipe-explanation"><strong>推荐理由</strong>{scenario.formulation.finalExplanation.summary}</p>
          </div>
        </div>
      </section>

      <section className="ht-s1-preview" aria-labelledby={`${id}-outcomes`}>
        <header className="ht-s1-preview-header"><div className="ht-s1-section-title"><span className="ht-s1-section-index">02</span><div><h3 id={`${id}-outcomes`}>四目标结果</h3><p>预测/模拟性质与目标阈值对照</p></div></div></header>
        <div className="ht-s6-outcome-layout">
          <div className="ht-s6-outcome-grid">{outcomes.map((outcome) => {
            const { target } = outcome;
            const Icon = AGENT_PROPERTY_ICONS[target.key];
            const unit = displayTargetUnit(target);
            return <article key={target.key} className="ht-s6-outcome" aria-label={`${target.shortLabel} 最终结果`} style={{ "--component-color": target.color } as CSSProperties}>
              <div className="ht-s6-property-label"><Icon aria-hidden="true" /><strong>{target.shortLabel}</strong><span>预测/模拟</span></div>
              <p className="ht-s6-property-value"><b>{formatValue(target, outcome.predictedValue)}</b><span>{unit}</span></p>
              <p className="ht-s6-threshold">目标 {target.direction === "lower" ? "≤" : "≥"} <b>{formatValue(target, target.target)}</b> {unit} · {target.direction === "lower" ? "越低越好" : "越高越好"}</p>
              <span className={cn("ht-s6-attainment", outcome.pass && "met")}>{outcome.pass ? <CheckCircle2 aria-hidden="true" /> : <Circle aria-hidden="true" />}{outcome.margin === 0 ? "恰好达到场景阈值" : outcome.pass ? "已达到场景阈值" : "尚未达到场景阈值"}</span>
              <p className="ht-s6-margin">{outcome.margin === 0 ? "与阈值相等" : <>{outcome.pass ? "达标余量" : "距达标差"} <b>{formatValue(target, Math.abs(outcome.margin))}</b> {unit}</>}</p>
            </article>;
          })}</div>
        </div>
      </section>

      <section className="ht-s1-preview" aria-labelledby={`${id}-sources`}>
        <header className="ht-s1-preview-header"><div className="ht-s1-section-title"><span className="ht-s1-section-index">03</span><div><h3 id={`${id}-sources`}>组分来源与结构追踪</h3><p>查看各组分的候选来源与分子结构</p></div></div><span className="ht-s6-caption ht-s6-source-caption" role="status" aria-live="polite" aria-atomic="true"><LockKeyhole aria-hidden="true" /><b>{source.component.id}</b> · 预设输出组分</span></header>
        <div className="ht-s6-source-body">
          <div className="ht-s6-source-tabs"><TargetTabs targets={targets} activeTargetKey={source.target.key} onSelectTarget={setActiveTargetKey} disabled={false}
            tabId={id} panelId={`${id}-source-detail`} label="选择最终配方组分"
            renderIcon={(target) => { const Icon = AGENT_PROPERTY_ICONS[target.key]; return <Icon aria-hidden="true" />; }}
            getTabLabel={(target) => `${sources.find((item) => item.target.key === target.key)!.component.id} ${target.shortLabel} 来源`}
            renderContent={(target) => { const item = sources.find((entry) => entry.target.key === target.key)!; const Icon = AGENT_PROPERTY_ICONS[target.key]; return <>
              <span className="ht-s6-source-title"><b>{item.component.id}</b><strong>{target.shortLabel}</strong><Icon aria-hidden="true" /></span>
              <span className="ht-s6-source-id">{item.candidate.id}</span><span className="ht-s6-source-description">{item.component.description}</span>
              <span className="ht-s6-source-ratio">配方占比 <span><b>{Math.round(item.ratio * 100)}</b><span>%</span></span></span>
            </>; }} /></div>
          <div className="ht-validation-workspace ht-s6-source-detail" id={`${id}-source-detail`} role="tabpanel" aria-labelledby={`${id}-${source.target.key}`} tabIndex={0} style={{ "--target-color": source.target.color } as CSSProperties}>
            <CandidateStructureDetails candidate={source.candidate} sourceLabel={`${source.trace.agentLabel} · ${source.trace.sourceStage}预设输出 · 组分 ${source.component.id}`} disabled={false} />
          </div>
        </div>
      </section>

      <section className="ht-s6-conclusion" aria-labelledby={`${id}-conclusion`}><FlaskConical aria-hidden="true" /><div><h3 id={`${id}-conclusion`}>{scenario.formulation.finalExplanation.nextStep}</h3><p>建议按此比例制备样品，并通过四性质测试确认性能。</p></div></section>
    </div>
    <footer ref={footerRef} className="ht-s1-footer ht-s6-footer"><button type="button" className="ht-s1-secondary-button" onClick={onBack}><ArrowLeft aria-hidden="true" />返回 S5 配比搜索</button><button type="button" className="ht-s1-primary-button" onClick={onRestart}><RotateCcw aria-hidden="true" />重新开始</button></footer>
  </div>;
}
