import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Activity, ArrowRight, ArrowUpRight, Atom, Bell, BookOpen, CalendarDays,
  ChevronRight, CircleCheck, Clock3, Cpu, FlaskConical, FolderKanban, Layers3,
  Lightbulb, Plus, RefreshCw, ScanLine, Sparkles, X, type LucideIcon
} from "lucide-react";
import type { ActiveModule } from "../routing";
import { useModalFocus } from "../hooks/useModalFocus";
import { useOverviewData } from "../hooks/useOverviewData";
import { ModulePageHeader } from "./ModulePageHeader";
import { OverviewDailySummary } from "./OverviewDailySummary";
import { WorkbenchSelect } from "./structure-workbench/WorkbenchSelect";
import { buildActivityDays, buildOverviewModel, dateText, dayKey, progressText, taskDetail, type OverviewDetail } from "./overviewPresentation";
import "./OverviewPage.css";

const shortcuts: { label: string; caption: string; icon: LucideIcon; module: ActiveModule }[] = [
  { label: "结构工作台", caption: "从一个结构开始", icon: ScanLine, module: "structureWorkbench" },
  { label: "知识检索", caption: "寻找研究线索", icon: BookOpen, module: "knowledge" },
  { label: "性质预测", caption: "评估材料潜力", icon: Activity, module: "homopolymerPrediction" },
  { label: "聚合物生成", caption: "探索更多可能", icon: Atom, module: "polytaoGeneration" },
  { label: "分子模拟", caption: "深入微观世界", icon: FlaskConical, module: "monomerMdSimulation" }
];
const numberText = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString();
const activityLabel = (source: string) => source === "0" ? "结构保存" : source === "1" ? "任务提交" : source === "2" ? "知识检索" : "研究活动";

function PanelHeading({ icon: Icon, title, children, id }: { icon: LucideIcon; title: string; children?: ReactNode; id?: string }) {
  return <div className="ov-panel-heading"><h2 id={id}><span className="ov-panel-title-icon"><Icon size={16} aria-hidden="true" /></span>{title}</h2>{children}</div>;
}

function Sparkline({ values, tone, id }: { values: number[]; tone: string; id: string }) {
  const peak = Math.max(1, ...values);
  const points = values.map((value, index) => `${index * 100 / Math.max(1, values.length - 1)},${42 - value / peak * 34}`).join(" ");
  return <svg className={`ov-sparkline ov-tone-${tone}`} viewBox="0 0 100 46" aria-hidden="true"><defs><linearGradient id={id} x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stopColor="currentColor" stopOpacity=".16" /><stop offset="100%" stopColor="currentColor" stopOpacity="0" /></linearGradient></defs><polygon points={`0,46 ${points} 100,46`} fill={`url(#${id})`} /><polyline points={points} fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" /></svg>;
}

function DetailDialog({ detail, onClose, onNavigate }: { detail: OverviewDetail; onClose: () => void; onNavigate: (module: ActiveModule) => void }) {
  const scope = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const trigger = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  useEffect(() => () => {
    requestAnimationFrame(() => {
      const target = trigger.current?.isConnected ? trigger.current : document.querySelector<HTMLButtonElement>('.ov-notice-tabs button[aria-pressed="true"]');
      if (target && !target.closest('[aria-hidden="true"], [inert]')) target.focus({ preventScroll: true });
    });
  }, []);
  useModalFocus({ active: true, open: true, scopeRef: scope, panelRef: panel, initialFocusRef: close, ownerId: "overview-detail", onClose });
  return <div className="ov-dialog-backdrop" ref={scope} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="ov-dialog np-workbench-theme" role="dialog" aria-modal="true" aria-labelledby="overview-detail-title" ref={panel}>
      <button className="ov-icon-button ov-dialog-close" type="button" aria-label="关闭详情" onClick={onClose} ref={close}><X size={18} /></button>
      <span className="ov-dialog-symbol"><Layers3 size={21} aria-hidden="true" /></span><span className="ov-eyebrow">{detail.eyebrow}</span><h2 id="overview-detail-title">{detail.title}</h2><p>{detail.description}</p>
      <dl>{detail.facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
      <div className="ov-dialog-footer"><span>来自当前平台记录</span><button className="ov-button ov-button-primary" type="button" onClick={() => { onClose(); onNavigate(detail.module); }}>{detail.action}<ArrowRight size={15} /></button></div>
    </section>
  </div>;
}

export function OverviewPage({ onNavigate }: { onNavigate: (module: ActiveModule) => void }) {
  const state = useOverviewData();
  const { data, loading, refreshing, access, refresh, error } = state;
  const [period, setPeriod] = useState<"7" | "30">("7");
  const [tasksOnly, setTasksOnly] = useState(false);
  const [detail, setDetail] = useState<OverviewDetail | null>(null);
  const [source, setSource] = useState("all");
  const [selectedDay, setSelectedDay] = useState(() => dayKey(new Date())!);
  const [hoveredDay, setHoveredDay] = useState<{ key: string; x: number; y: number } | null>(null);
  const detailSnapshot = useRef(data);
  const activityRef = useRef<HTMLElement>(null);
  const heatmapScrollRef = useRef<HTMLDivElement>(null);
  const model = useMemo(() => buildOverviewModel(data, Number(period)), [data, period]);
  const days = useMemo(() => buildActivityDays(model, source), [model, source]);
  const selected = days.find(day => day.key === selectedDay && day.inRange) ?? days.find(day => day.key === model.today)!;
  const hovered = hoveredDay ? days.find(day => day.key === hoveredDay.key) : null;
  const events = model.events.filter(event => !tasksOnly || event.kind === "task").slice(0, 3);
  const available = access === "member";
  const taskComplete = model.taskReady;
  const activityAvailable = days.some(day => day.inRange && day.count !== null);
  const activityTotal = days.filter(day => day.inRange).reduce((sum, day) => sum + (day.count ?? 0), 0);
  const activeDays = days.filter(day => day.inRange && (day.count ?? 0) > 0).length;
  const activityPartial = source === "all" && (!model.structureActivityReady || !model.taskActivityReady || !model.knowledgeDailyReady);
  const missingSources = data ? [data.structures.status !== "ready", data.taskSources.some(item => item.status !== "ready"), data.knowledge.status !== "ready", data.knowledgeActivity.status !== "ready"].some(Boolean) : false;
  const date = new Date(data?.asOf ?? new Date());
  const metrics = [
    { title: "已记录计算任务", value: model.totalTasks, unit: "项", foot: "单体 MD / DFT · 聚合物 MD / 聚合批任务", change: model.submittedInPeriod === null ? "统计暂不可用" : `近 ${period} 天提交 ${model.submittedInPeriod}`, icon: FolderKanban, tone: "blue", values: model.taskSeries },
    { title: "进行中计算", value: model.runningCount, unit: "项", foot: "包含排队、执行和取消中的正式任务", change: taskComplete ? `已完成 ${model.completed.length} 项` : "状态暂不可用", icon: Cpu, tone: "cyan", values: null },
    { title: "已保存结构", value: model.savedCount, unit: "个", foot: "当前账号的结构库记录", change: model.structuresInPeriod === null ? "统计暂不可用" : `近 ${period} 天新增 ${model.structuresInPeriod}`, icon: Atom, tone: "violet", values: model.structureSeries },
    { title: model.literature ? "知识库文献" : "知识库条目", value: model.primaryKnowledge?.total ?? null, unit: model.literature ? "篇" : "条", foot: "平台共享知识库 · 与个人检索记录分开统计", change: model.literature && model.legacy ? `另有 ${numberText(model.legacy.total)} 条知识` : model.primaryKnowledge ? "已发布的知识资源" : "知识库暂不可用", icon: BookOpen, tone: "amber", values: null }
  ];
  useEffect(() => { if (data) { setDetail(null); setHoveredDay(null); } }, [data]);
  useEffect(() => { if (!available) { setDetail(null); setHoveredDay(null); } }, [available]);
  useEffect(() => { if (selected.key !== selectedDay) setSelectedDay(selected.key); }, [selected.key, selectedDay]);
  function openDetail(value: OverviewDetail) { detailSnapshot.current = data; setDetail(value); }
  function showActivityTooltip(key: string, target: HTMLButtonElement) {
    const panel = activityRef.current?.getBoundingClientRect();
    if (!panel) return;
    const cell = target.getBoundingClientRect();
    setHoveredDay({ key, x: Math.max(108, Math.min(panel.width - 108, cell.left - panel.left + cell.width / 2)), y: cell.top - panel.top - 8 });
  }
  function focusActivityDay(target: HTMLButtonElement) {
    target.focus({ preventScroll: true });
    const scroll = heatmapScrollRef.current;
    if (!scroll?.clientWidth) return;
    const bounds = scroll.getBoundingClientRect(), cell = target.getBoundingClientRect();
    const left = bounds.left + scroll.clientLeft, right = left + scroll.clientWidth;
    if (cell.left - 4 < left) scroll.scrollLeft += cell.left - 4 - left;
    else if (cell.right + 4 > right) scroll.scrollLeft += cell.right + 4 - right;
  }
  const unavailableText = loading ? "正在读取最新数据…" : !available ? access === "restricted" ? "当前账号暂无科研数据访问权限" : "登录后查看个人研发记录" : "数据暂时不可用，可稍后刷新重试";
  return <section className="np-module-page ov-page" data-testid="overview-page" aria-busy={loading}>
    <ModulePageHeader actions={<div className="ov-header-meta"><span className="ov-preview-tag" data-state={!available ? "idle" : missingSources || error ? "partial" : "ready"}><span />{loading || refreshing ? "正在同步数据" : !available ? "登录后查看" : missingSources || error ? "部分数据可用" : "数据已更新"}</span><span className="ov-header-date"><CalendarDays size={13} aria-hidden="true" />{date.toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" })}<span>{date.toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai", weekday: "long" })}</span></span></div>}>系统总览</ModulePageHeader>
    <div className="np-module-page-body ov-scroll" onScroll={() => setHoveredDay(null)}><div className="ov-content">
      <div className="ov-intro"><p>{data ? `上次更新 ${date.toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false })} · 个人结构与任务，平台共享知识库` : unavailableText}</p><div className="ov-intro-actions"><div className="ov-period" data-period={period} aria-label="指标统计周期">{(["7", "30"] as const).map(value => <button key={value} type="button" aria-pressed={period === value} onClick={() => setPeriod(value)}>近 {value} 天</button>)}</div><button className="ov-button ov-button-primary" type="button" onClick={() => onNavigate("structureWorkbench")}><Plus size={15} />开始新探索</button></div></div>
      {!loading && (!available || error || missingSources) && <div className="ov-data-message" role="status"><span>{!available ? unavailableText : error ? "刷新未完成，正在保留上次可用数据。" : "部分数据暂不可用，以下保留可读取内容；缺失统计以 — 显示。"}</span>{access === "guest" ? <button type="button" onClick={() => window.dispatchEvent(new Event("nexpoly:login-required"))}>登录账号<ArrowRight size={12} /></button> : available && <button type="button" disabled={refreshing} onClick={refresh}>重新读取<RefreshCw size={12} /></button>}</div>}
      <div className="ov-main-grid">
        <section className="ov-metrics-section" aria-label="研发关键指标"><div className="ov-metrics">{metrics.map(({ title, value, unit, change, foot, icon: Icon, tone, values }, index) => <article className={`ov-metric ov-tone-${tone}`} key={index}><div className="ov-metric-label"><span className="ov-metric-icon"><Icon size={16} /></span><h2>{title}</h2></div><div className="ov-metric-value"><strong>{numberText(value)}</strong><span>{unit}</span>{values && <Sparkline values={values} tone={tone} id={`ov-trend-${index}`} />}</div><div className="ov-metric-footer"><span title={foot}>{foot}</span><span className="ov-metric-change">{change}</span></div></article>)}</div></section>
        <section className="ov-panel ov-notifications" aria-labelledby="ov-notifications-title"><div className="ov-panel-heading"><h2 id="ov-notifications-title"><span className="ov-panel-title-icon"><Bell size={16} /></span>近期动态</h2><button type="button" className={`ov-icon-button${refreshing ? " is-refreshing" : ""}`} title="刷新总览数据" aria-label="刷新总览数据" disabled={!available || loading || refreshing} onClick={refresh}><RefreshCw size={15} /></button></div><div className="ov-notice-tabs"><button type="button" aria-pressed={!tasksOnly} onClick={() => setTasksOnly(false)}>全部动态</button><button type="button" aria-pressed={tasksOnly} onClick={() => setTasksOnly(true)}>计算任务</button><span className="ov-notice-caption">按更新时间排序</span></div><div className="ov-notice-list">{events.map(event => {
          const Icon = event.kind === "structure" ? Atom : event.kind === "knowledge" ? BookOpen : Activity;
          return <button className="ov-notice" type="button" key={event.id} onClick={() => openDetail(event.detail)}><span className={`ov-notice-icon ov-tone-${event.tone}`}><Icon size={15} /></span><span className="ov-notice-body"><span className="ov-notice-topline"><strong>{event.title}</strong><time dateTime={event.at}>{dateText(event.at)}</time></span><span>{event.description}</span></span><ChevronRight size={14} /></button>;
        })}{!events.length && <div className="ov-empty"><Bell size={22} /><strong>{loading || !available ? unavailableText : missingSources ? "暂无可读取的近期动态" : "暂无研发动态"}</strong>{available && !loading && <span>保存结构、提交计算或检索知识后可在这里查看</span>}</div>}</div></section>
        <section className="ov-panel ov-summary" aria-labelledby="ov-summary-title"><PanelHeading id="ov-summary-title" icon={Sparkles} title="今日研发摘要"><span className="ov-small-tag">{model.today.slice(5).replace("-", " 月 ")} 日 · 当前账号</span></PanelHeading><OverviewDailySummary model={model} loading={loading} available={available} onNavigate={onNavigate} onOpenDetail={openDetail} /></section>
        <section className="ov-panel ov-projects" aria-labelledby="ov-projects-title"><PanelHeading id="ov-projects-title" icon={FolderKanban} title="最近计算任务"><span className="ov-small-tag"><span className="ov-status-dot" />{model.runningCount === null ? "部分状态待同步" : `${model.runningCount} 项进行中`}</span></PanelHeading><div className="ov-project-list">{model.recentTasks.map(task => <button type="button" className={`ov-project ov-tone-${task.outcome === "attention" ? "amber" : task.source === "monomerMd" ? "cyan" : "blue"}`} key={task.id} onClick={() => openDetail(taskDetail(task))}><span className="ov-project-symbol">{task.source === "monomerMd" ? "MD" : task.source === "monomerDft" ? "DFT" : task.source === "polymerMd" ? "PM" : "PB"}</span><span className="ov-project-content"><span className="ov-project-title"><strong title={task.title}>{task.title}</strong><ChevronRight size={13} /></span><span className="ov-project-meta"><span>{task.statusLabel}</span><span>{task.progress === null ? dateText(task.updatedAt ?? task.createdAt) : progressText(task.progress)}</span></span>{task.progress !== null ? <span className="ov-progress" role="progressbar" aria-label={`${task.title}进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={task.progress}><span style={{ width: progressText(task.progress) }} /></span> : <span className="ov-task-stage">{task.stage ?? "阶段进度由对应模块提供"}</span>}</span></button>)}{!model.recentTasks.length && <div className="ov-empty"><Cpu size={22} /><strong>{loading || !available ? unavailableText : taskComplete ? "暂无计算任务" : "任务记录暂不可用"}</strong>{available && !loading && <span>提交正式计算后可在这里跟进</span>}</div>}</div><div className="ov-project-bottom"><CircleCheck size={13} />{taskComplete ? `${model.completed.length} 项已完成` : "仅展示可读取任务"}<span /><Clock3 size={13} />{taskComplete ? `${model.attention.length} 项需关注` : "缺失状态不计为 0"}</div></section>
      </div>
      <section className="ov-panel ov-activity" ref={activityRef}><PanelHeading icon={Activity} title="研究活跃度"><div className="ov-source-filter"><WorkbenchSelect id="overview-activity-source" ariaLabel="活动来源" value={source} onChange={setSource} constrainToScrollParent placement="bottom" options={[{ value: "all", label: "全部活动" }, { value: "0", label: "结构保存" }, { value: "1", label: "计算任务" }, { value: "2", label: "知识检索" }]} /></div></PanelHeading><div className="ov-activity-body"><div className="ov-activity-summary"><strong>{activityAvailable ? numberText(activityTotal) : "—"}<span>次{activityLabel(source)}</span></strong><span>{activityPartial ? "可用来源" : "过去一年"}<span className="ov-summary-separator">/</span>活跃 <b>{activityAvailable ? activeDays : "—"}</b> 天</span><span className="ov-activity-year">{days.find(day => day.inRange)!.key.slice(0, 7).replace("-", ".")} — {model.today.slice(0, 7).replace("-", ".")}</span></div>
        {activityAvailable ? <><div className="ov-heatmap-scroll" ref={heatmapScrollRef} onScroll={() => setHoveredDay(null)}><div className="ov-heatmap-layout"><div className="ov-week-labels"><span>一</span><span>三</span><span>五</span></div><div className="ov-heatmap"><div className="ov-months">{Array.from({ length: 53 }, (_, week) => { const day = days[week * 7], previous = days[(week - 1) * 7]; return <span key={week}>{week === 0 || day.date.getUTCMonth() !== previous.date.getUTCMonth() ? `${day.date.getUTCMonth() + 1} 月` : ""}</span>; })}</div><div className="ov-heatmap-cells" aria-label="过去一年每日研究活动，使用方向键选择日期" onPointerLeave={() => setHoveredDay(null)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setHoveredDay(null); }} onKeyDown={event => {
          if (event.key === "Escape") { event.preventDefault(); setHoveredDay(null); return; }
          const offsets: Record<string, number> = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1 };
          if (!(event.key in offsets)) return;
          event.preventDefault(); const current = days.findIndex(day => day.key === selected.key), next = days[current + offsets[event.key]];
          if (!next?.inRange) return;
          setSelectedDay(next.key); const target = event.currentTarget.querySelector<HTMLButtonElement>(`[data-date="${next.key}"]`); if (target) focusActivityDay(target);
        }}>{days.map(day => day.inRange ? <button type="button" key={day.key} data-date={day.key} data-level={day.level} tabIndex={selected.key === day.key ? 0 : -1} aria-label={`${day.key}，${numberText(day.count)} 次${activityLabel(source)}`} aria-pressed={selected.key === day.key} aria-describedby={hoveredDay?.key === day.key ? "ov-activity-tooltip" : undefined} onPointerEnter={event => { if (event.pointerType === "mouse") showActivityTooltip(day.key, event.currentTarget); }} onFocus={event => showActivityTooltip(day.key, event.currentTarget)} onClick={() => setSelectedDay(day.key)} /> : <span key={day.key} />)}</div></div></div></div><div className="ov-heatmap-footer"><span className="ov-day-detail" aria-live="polite"><span className="ov-status-dot" />{selected.key.replaceAll("-", ".")}<span>·</span><strong>{numberText(selected.count)} 次{activityLabel(source)}</strong><span className="ov-day-breakdown" aria-label="当日分类统计"><span>结构 <b>{numberText(selected.breakdown[0])}</b></span><span>计算 <b>{numberText(selected.breakdown[1])}</b></span><span>检索 <b>{numberText(selected.breakdown[2])}</b></span></span></span><span className="ov-heatmap-legend">少{[0, 1, 2, 3, 4].map(level => <i data-level={level} key={level} />)}多</span></div></> : <div className="ov-activity-empty">{loading || !available ? unavailableText : "该来源的活动记录暂不可用"}</div>}
        <p className="ov-activity-note">按现存结构保存、正式任务提交及已采集知识检索统计；不含演示和已删除记录，检索历史不补记。</p>
      </div>{hoveredDay && hovered && <div id="ov-activity-tooltip" role="tooltip" className="ov-activity-tooltip" style={{ left: hoveredDay.x, top: hoveredDay.y }}><span>{hovered.date.toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric", timeZone: "UTC" })}</span><strong>{numberText(hovered.count)} <small>次{activityLabel(source)}</small></strong><span className="ov-tooltip-breakdown">结构 {numberText(hovered.breakdown[0])}<i />计算 {numberText(hovered.breakdown[1])}<i />检索 {numberText(hovered.breakdown[2])}</span></div>}</section>
      <section className="ov-quick-section" aria-label="常用工具"><div className="ov-section-toolbar"><span>开始下一次探索</span><span className="ov-subtle">从想法，到材料</span></div><div className="ov-shortcuts">{shortcuts.map(({ label, caption, icon: Icon, module }) => <button type="button" key={module} onClick={() => onNavigate(module)}><span className="ov-shortcut-icon"><Icon size={18} aria-hidden="true" /></span><span><strong>{label}</strong><small>{caption}</small></span><ArrowUpRight size={14} /></button>)}</div></section>
      <footer className="ov-page-footer"><span>NEXPOLY<span className="ov-footer-divider">/</span>智聚万物</span><span><Lightbulb size={12} />让聚合物研发，更进一步</span></footer>
    </div></div>{detail && data && detailSnapshot.current === data && available && <DetailDialog detail={detail} onClose={() => setDetail(null)} onNavigate={onNavigate} />}
  </section>;
}
