import { ArrowLeft, BadgeInfo, Check, CheckCircle2, ChevronRight, Circle, Eye, Flame, GitBranch, Layers3, LoaderCircle, LockKeyhole, Play, Star, X } from "lucide-react";
import { useId, useLayoutEffect, useRef, type CSSProperties, type KeyboardEvent } from "react";
import { highThroughputDemoScenario, type HighThroughputTarget, type HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";
import { cn } from "../../lib/utils";
import { AGENT_PROPERTY_ICONS } from "./PriorWorkbench";
import { displayTargetUnit } from "./prior-hotspot-model";
import { formatIterationValue as formatValue } from "./iteration-model";
import { buildRatioSearchSummary, ratioValidationNumber, type RatioMixCandidate, type RatioValidationValues, type RatioValidationConfirmationState } from "./ratio-search-model";
import { RatioSearchMap } from "./RatioSearchMap";
import "./ratio-search-workspace.css";

type Props = {
  targets: HighThroughputTarget[];
  progressStep: number;
  viewStep: number;
  onViewStep: (step: number) => void;
  validationValues: RatioValidationValues;
  confirmations: RatioValidationConfirmationState;
  onValidationValueChange: (target: HighThroughputTargetKey, value: string) => void;
  onConfirm: () => void;
  onBack: () => void;
  onNext: () => void;
  canAdvance: boolean;
  transitionMessage: string | null;
};
const formulation = highThroughputDemoScenario.formulation;

function RatioComposition({ mix }: { mix: RatioMixCandidate }) {
  return <div className="ht-s5-composition">
    <div className="ht-s5-ratio-bar" aria-hidden="true">{formulation.components.map((component) => <span key={component.id} style={{ width: `${mix.ratios[component.id] * 100}%`, backgroundColor: component.color }} />)}</div>
    <dl>{formulation.components.map((component) => <div key={component.id} style={{ "--component-color": component.color } as CSSProperties}><dt><i />{component.id}</dt><dd>{Math.round(mix.ratios[component.id] * 100)}<small>%</small></dd></div>)}</dl>
  </div>;
}

export function RatioSearchWorkspace({ targets, progressStep, viewStep, onViewStep, validationValues, confirmations,
  onValidationValueChange, onConfirm, onBack, onNext, canAdvance, transitionMessage }: Props) {
  const id = useId();
  const footerRef = useRef<HTMLElement>(null);
  const roundRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLElement>(null);
  const previousViewStep = useRef(viewStep);
  const viewedMixId = formulation.searchSteps[viewStep].proposedMixId;
  const confirmed = Boolean(confirmations[viewedMixId]);
  const summary = buildRatioSearchSummary(viewStep, confirmed);
  const { step, proposed, previous, current, isSeed, decisionVisible } = summary;
  const review = viewStep < progressStep;
  const busy = Boolean(transitionMessage);
  const missing = isSeed ? [] : targets.filter((target) => ratioValidationNumber(validationValues, proposed.id, target.key) === null);
  const lastStep = viewStep === formulation.searchSteps.length - 1;
  const status = review ? <>回看模式 · <b>T{viewStep}</b> 已完成</> : isSeed ? "起点已就绪 · 可启动搜索" : missing.length
    ? <>缺 <b>{missing.length}</b> 项：{missing.map((target) => target.shortLabel).join("、")}</>
    : confirmed ? <>本步 <b>4</b> 项已确认</> : <>已填 <b>4/4</b> · 待确认</>;
  const decision = !decisionVisible ? "待确认后展示" : isSeed ? "设为初始解" : step.accepted ? lastStep ? "锁定预设候选" : "接受新解" : "拒绝邻域解";

  useLayoutEffect(() => {
    const footer = footerRef.current;
    const scroll = footer?.closest<HTMLElement>(".ht-scroll-region");
    if (!footer || !scroll) return;
    const nav = scroll.querySelector<HTMLElement>(".ht-flow-steps");
    const oldLeft = nav?.scrollLeft ?? 0;
    const sync = () => {
      scroll.style.setProperty("--ht-s5-footer-height", `${footer.offsetHeight}px`);
      const active = nav?.querySelector<HTMLElement>('[aria-current="step"]');
      if (!nav || !active || nav.scrollWidth <= nav.clientWidth) return;
      const bounds = nav.getBoundingClientRect();
      const item = active.getBoundingClientRect();
      if (item.left < bounds.left || item.right > bounds.right) nav.scrollLeft += item.left - bounds.left - (bounds.width - item.width) / 2;
    };
    sync();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(sync);
    observer?.observe(footer);
    if (nav) observer?.observe(nav);
    return () => { observer?.disconnect(); scroll.style.removeProperty("--ht-s5-footer-height"); if (nav) nav.scrollLeft = oldLeft; };
  }, []);

  useLayoutEffect(() => {
    if (previousViewStep.current !== viewStep) {
      const panel = searchRef.current;
      const scroll = panel?.closest<HTMLElement>(".ht-scroll-region");
      if (panel && scroll) {
        const top = panel.getBoundingClientRect().top;
        const regionTop = scroll.getBoundingClientRect().top;
        if (top < regionTop) scroll.scrollTop += top - regionTop - 12;
      }
      roundRef.current?.querySelector<HTMLButtonElement>(`[data-step="${viewStep}"]`)?.focus({ preventScroll: true });
    }
    previousViewStep.current = viewStep;
  }, [viewStep]);

  function selectStep(index: number, focus = false) {
    if (busy || index < 0 || index > progressStep) return;
    onViewStep(index);
    if (focus) roundRef.current?.querySelector<HTMLButtonElement>(`[data-step="${index}"]`)?.focus({ preventScroll: true });
  }
  function onStepKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const next = event.key === "Home" ? 0 : event.key === "End" ? progressStep : ["ArrowRight", "ArrowDown"].includes(event.key)
      ? (index + 1) % (progressStep + 1) : ["ArrowLeft", "ArrowUp"].includes(event.key) ? (index + progressStep) % (progressStep + 1) : null;
    if (next === null) return;
    event.preventDefault();
    selectStep(next, true);
  }

  return <div className="ht-s1-stage ht-s5-stage" aria-busy={busy}>
    <div className="ht-s1-content">
      <div className="ht-workbench-demo-note"><BadgeInfo aria-hidden="true" size={16} /><span><strong>演示说明：</strong>当前按预设路径演示模拟退火。验证值可编辑并用于演示回流记录，不重算得分、接受概率或决策，也不改变 S6 最终配方。</span></div>
      <div className="ht-s1-summary"><div className="ht-s1-scenario-facts"><Layers3 aria-hidden="true" /><span><b>p1–p4</b> 固定组分</span><span>比例步长 <b>10%</b></span><span><b>{formulation.ratioGrid.candidateCount}</b> 组配比</span></div>
        <span className={cn("ht-s5-status", review && "review")} role="status" aria-live="polite" aria-atomic="true">{review ? <Eye aria-hidden="true" /> : <GitBranch aria-hidden="true" />}<span>{review ? status : <><b>T{viewStep}</b> · {step.label} · {status}</>}</span></span></div>

      <section className="ht-s1-preview" aria-labelledby={`${id}-components`}>
        <header className="ht-s1-preview-header"><div className="ht-s1-section-title"><span className="ht-s1-section-index">01</span><div><h3 id={`${id}-components`}>固定组分与目标权重</h3><p>承接 S4 预设输出；比例可变，组分与评分脚本固定</p></div></div><span className="ht-s5-caption"><LockKeyhole aria-hidden="true" />S4 → S5</span></header>
        <div className="ht-s5-components">{formulation.components.map((component) => {
          const target = targets.find((item) => item.key === component.sourceTargetKey)!;
          const Icon = AGENT_PROPERTY_ICONS[target.key];
          return <article key={component.id} style={{ "--component-color": component.color } as CSSProperties}>
            <div className="ht-s5-component-title"><b>{component.id}</b><h4>{target.shortLabel}</h4><Icon aria-hidden="true" /></div>
            <strong>{component.candidateId}</strong><p>{component.description}</p><span>目标权重 <b>{target.weight}%</b></span>
          </article>;
        })}</div>
      </section>

      <section ref={searchRef} className="ht-s1-preview ht-s5-search-panel" aria-labelledby={`${id}-search`}>
        <header className="ht-s1-space-header"><div className="ht-s1-section-title"><span className="ht-s1-section-index">02</span><div><h3 id={`${id}-search`}>比例空间与退火路径</h3><p>完成步骤可只读回看；图中只展示所选步骤的证据</p></div></div><span className="ht-s5-caption"><Flame aria-hidden="true" />模拟退火</span></header>
        <div className="ht-s5-search-layout"><RatioSearchMap summary={summary} />
          <aside className="ht-s5-timeline-panel"><h4>五步预设搜索</h4><nav ref={roundRef} className="ht-s5-step-nav" aria-label="模拟退火步骤">
            {formulation.searchSteps.map((item, index) => {
              const done = index < progressStep;
              return <button key={item.id} type="button" data-step={index} className={cn(index === viewStep && "selected", done && "completed")}
                disabled={busy || index > progressStep} aria-pressed={index === viewStep} aria-current={index === progressStep ? "step" : undefined}
                aria-label={`查看 T${index} ${item.label}`} onClick={() => selectStep(index)} onKeyDown={(event) => onStepKeyDown(event, index)}>
                <span className="ht-s5-step-index">{done ? <Check aria-hidden="true" /> : `T${index}`}</span><span><strong>{item.label}</strong><small>{index <= progressStep ? <><b>{item.proposedMixId}</b> · 温度 <b>{item.temperature.toFixed(2)}</b></> : "等待前一步完成"}</small></span>
                <em>{index > progressStep ? "未开始" : index === viewStep && review ? "回看" : done ? "已完成" : "当前"}</em>
              </button>;
            })}</nav><p className="ht-s5-timeline-note"><Eye aria-hidden="true" />回看不改变当前搜索进度</p></aside>
        </div>
        <div className="ht-s5-decision" aria-label={`T${viewStep} 预设决策`}>
          <dl className="ht-s5-decision-metrics"><div><dt>温度 T</dt><dd>{step.temperature.toFixed(2)}</dd></div><div><dt>预设 Δscore</dt><dd>{step.deltaScore > 0 ? "+" : ""}{step.deltaScore}</dd></div><div><dt>预设接受概率</dt><dd>{Math.round(step.acceptanceProbability * 100)}<small>%</small></dd></div><div><dt>本步决策</dt><dd className={cn("ht-s5-decision-label", decisionVisible && (step.accepted ? "accepted" : "rejected"))}>{!decisionVisible ? <Circle aria-hidden="true" /> : step.accepted ? <CheckCircle2 aria-hidden="true" /> : <X aria-hidden="true" />}<span>{decision}</span></dd></div></dl>
          <p><BadgeInfo aria-hidden="true" /><span><strong>预设决策：</strong>{decisionVisible ? step.description : "确认本步四项演示验证值后，展示该步骤的预设接受／拒绝决策。"} 温度、分差与概率均来自脚本，不由输入值计算。</span></p>
        </div>
      </section>

      <section className="ht-s1-preview ht-s5-validation" aria-labelledby={`${id}-validation`}>
        <header className="ht-s1-preview-header"><div className="ht-s1-section-title"><span className="ht-s1-section-index">03</span><div><h3 id={`${id}-validation`}>{isSeed ? "初始配方与搜索起点" : "本步比例对照与验证"}</h3><p>{isSeed ? "起点无需录入；启动后逐步确认四性质验证值" : review ? "已完成步骤 · 验证记录只读，不能修改" : "已预填示例值，可修改；有效预填不等于本步已确认"}</p></div></div><span className="ht-s5-caption"><b>{proposed.id}</b> · {isSeed ? "固定起点" : "本步验证配方"}</span></header>
        <div className="ht-s5-validation-body">
          <div className={cn("ht-s5-mix-comparison", isSeed && "seed")}>
            {(isSeed ? [{ mix: proposed, label: "预设初始解", focus: true }] : [{ mix: previous, label: "扰动前", focus: false }, { mix: proposed, label: "本步提议", focus: true }, { mix: current, label: decisionVisible ? "决策后保留" : "当前保留 · 待确认", focus: false }]).map(({ mix, label, focus }) =>
              <article className={cn("ht-s5-mix-card", focus && "proposed")} key={label} aria-label={`${label} ${mix.id}`}><header><span>{label}</span>{focus ? <GitBranch aria-hidden="true" /> : <Layers3 aria-hidden="true" />}</header><div className="ht-s5-mix-heading"><strong>{mix.id}</strong><span>预设综合分 <b>{mix.score}</b></span></div><RatioComposition mix={mix} /></article>)}
            {isSeed && <div className="ht-s5-seed-note"><Play aria-hidden="true" /><div><h4>从起点开始邻域搜索</h4><p>依次展示两次接受、一次拒绝与最终选择。每个后续步骤仅确认该配方的四项演示验证值；不执行在线优化或真实实验。</p></div></div>}
          </div>
          {!isSeed && <div className="ht-s5-validation-grid" aria-label={`${proposed.id} 四性质演示验证值`}>
            {targets.map((target) => {
              const Icon = AGENT_PROPERTY_ICONS[target.key];
              const value = ratioValidationNumber(validationValues, proposed.id, target.key);
              const meets = value !== null && (target.direction === "lower" ? value <= target.target : value >= target.target);
              const fieldId = `${id}-${target.key}`;
              return <div key={target.key} className="ht-s5-validation-card" style={{ "--component-color": target.color } as CSSProperties}>
                <label htmlFor={review ? undefined : fieldId}><Icon aria-hidden="true" /><strong>{target.shortLabel}</strong><span>{review ? "验证记录" : "演示验证值"}</span></label>
                {review ? <p className="ht-s5-readonly-value"><b>{value === null ? "—" : formatValue(target, value)}</b><span>{displayTargetUnit(target)}</span></p>
                  : <div className={cn("ht-s5-input-wrap", value === null && "invalid")}><input id={fieldId} type="number" step={target.key === "modulus" ? "0.1" : "any"} inputMode="decimal"
                    aria-label={`${proposed.id} ${target.shortLabel} 演示验证值`} aria-invalid={value === null} aria-describedby={`${fieldId}-threshold${value === null ? ` ${fieldId}-error` : ""}`}
                    value={validationValues[proposed.id]?.[target.key] ?? ""} disabled={busy} onChange={(event) => onValidationValueChange(target.key, event.target.value)} /><span>{displayTargetUnit(target)}</span></div>}
                <p id={`${fieldId}-threshold`} className="ht-s5-field-threshold">目标 {target.direction === "lower" ? "≤" : "≥"} <b>{formatValue(target, target.target)}</b> {displayTargetUnit(target)} · {target.direction === "lower" ? "越低越好" : "越高越好"}</p>
                {value === null ? <p className="ht-s5-field-error" role="alert" id={`${fieldId}-error`}>请输入有限数值</p> : <p className={cn("ht-s5-field-state", meets && "met")}>{meets ? <CheckCircle2 aria-hidden="true" /> : <Circle aria-hidden="true" />}{meets ? "验证值达到场景阈值" : "验证值未达场景阈值"}</p>}
              </div>;
            })}
          </div>}
          {!isSeed && <p className="ht-s5-validation-note"><LockKeyhole aria-hidden="true" /><span>达标提示按上方验证值与 S0 阈值比较；预设综合分与最终配方不会随之重算。</span></p>}
        </div>
      </section>
    </div>
    <footer ref={footerRef} className={cn("ht-s1-footer ht-s5-footer", review && "review")}>
      {review ? <><p className="ht-s5-status"><Eye aria-hidden="true" />回看模式 · 当前进度为 <b>T{progressStep}</b></p><button type="button" className="ht-s1-primary-button" onClick={() => selectStep(progressStep, true)}>返回当前步骤<ChevronRight aria-hidden="true" /></button></>
        : <><button type="button" className="ht-s1-secondary-button" onClick={onBack} disabled={busy}><ArrowLeft aria-hidden="true" />返回 S4 候选输出</button>
          <p className={cn("ht-s5-status", confirmed && !isSeed && "confirmed")} role="status" aria-live="polite" aria-atomic="true">{busy ? <LoaderCircle aria-hidden="true" className="ht-s1-spinner" /> : confirmed ? <CheckCircle2 aria-hidden="true" /> : <Circle aria-hidden="true" />}<span>{transitionMessage || status}</span></p>
          <div className="ht-s5-batch-actions">{!isSeed && <button type="button" className="ht-s1-secondary-button" onClick={onConfirm} disabled={busy || missing.length > 0 || confirmed}>{confirmed ? <CheckCircle2 aria-hidden="true" /> : <Check aria-hidden="true" />}{confirmed ? "本步已确认" : "确认本步 4 项验证值"}</button>}
            <button type="button" className="ht-s1-primary-button" onClick={onNext} disabled={busy || !canAdvance}>{busy ? <LoaderCircle aria-hidden="true" className="ht-s1-spinner" /> : isSeed ? <Play aria-hidden="true" /> : lastStep ? <Star aria-hidden="true" /> : null}{busy ? "正在演示验证回流" : isSeed ? "开始邻域搜索" : lastStep ? "进入 S6 最终解释" : `进入 T${viewStep + 1} ${formulation.searchSteps[viewStep + 1].label}`}{!busy && <ChevronRight aria-hidden="true" />}</button></div></>}
    </footer>
  </div>;
}
