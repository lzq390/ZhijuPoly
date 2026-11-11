import { Activity, ArrowRight, Atom, BookOpen, CalendarCheck2, CircleAlert, Lightbulb } from "lucide-react";
import type { ActiveModule } from "../routing";
import type { OverviewDetail, OverviewModel } from "./overviewPresentation";
import { progressText, taskDetail } from "./overviewPresentation";
import "./OverviewDailySummary.css";

type Props = {
  model: OverviewModel;
  loading: boolean;
  available: boolean;
  onNavigate: (module: ActiveModule) => void;
  onOpenDetail: (detail: OverviewDetail) => void;
};
const countText = (value: number | null) => value === null ? "—" : String(value).padStart(2, "0");

export function OverviewDailySummary({ model, loading, available, onNavigate, onOpenDetail }: Props) {
  const result = model.latestCompleted;
  const issue = model.latestAttention;
  const running = model.active.slice(0, 2);
  const advice = loading ? "正在整理最新的结构、计算任务和知识记录…" : !available ? "登录并获得数据访问权限后，可在这里查看当前账号的研发摘要。" : result ? result.outcome === "attention" ? `${result.title}已结束，但结果仍需关注；建议先查看详情并复核，再安排后续研究。` : `${result.title}已完成，可先查看计算结果，再安排下一步结构设计。` : model.active.length ? `当前可读取到 ${model.active.length} 项进行中计算，可在对应模块查看最新阶段。` : model.savedCount ? "从已保存的结构出发，结合知识检索安排下一轮材料研究。" : "从绘制材料结构开始，保存研究对象后再发起计算或查找文献线索。";
  return <div className="ov-daily-summary" aria-busy={loading}>
    <div className="ov-daily-layout">
      <section className="ov-daily-output" aria-label="今日产出与计算进展">
        <div className="ov-daily-heading"><h3>今日产出</h3><span className="ov-daily-example">当前账号</span></div>
        <dl className="ov-daily-totals">
          <div><dt>今日完成</dt><dd className="ov-daily-success">{countText(model.completedToday)}</dd></div>
          <div><dt>运行中</dt><dd className="ov-daily-blue">{countText(model.runningCount)}</dd></div>
          <div><dt>需关注</dt><dd className="ov-daily-warning">{countText(model.taskReady ? model.attention.length : null)}</dd></div>
        </dl>
        <div className="ov-daily-additions" aria-label="今日新增">
          <span><Atom size={12} aria-hidden="true" />新增 <strong>{model.todayStructures ?? "—"}</strong> 个结构</span>
          <span><BookOpen size={12} aria-hidden="true" />检索 <strong>{model.todayKnowledge ?? "—"}</strong> 次知识</span>
        </div>
        <div className="ov-daily-running-heading"><h3>计算进展</h3><span>{running.length ? `显示 ${running.length} 项进行中任务` : "正式计算任务"}</span></div>
        <ul className="ov-daily-running">
          {running.map(task => <li className={`ov-daily-task ov-daily-task-${task.source === "monomerMd" ? "cyan" : "blue"}`} key={task.id}>
            <Activity size={14} aria-hidden="true" />
            <div className="ov-daily-task-body"><div className="ov-daily-task-label"><strong title={task.title}>{task.title}</strong><span>{task.progress === null ? task.statusLabel : progressText(task.progress)}</span></div>
              {task.progress !== null ? <div className="ov-daily-task-track" role="progressbar" aria-label={`${task.title}进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={task.progress}><span style={{ width: progressText(task.progress) }} /></div> : <span className="ov-daily-task-stage">{task.stage ?? "等待计算模块更新阶段"}</span>}
            </div>
          </li>)}
        </ul>
        {!running.length && <div className="ov-daily-no-tasks">{loading ? "正在读取任务状态…" : !available ? "登录后查看计算进展" : model.taskReady ? "当前没有运行中的计算任务" : "部分任务数据暂不可用"}</div>}
      </section>
      <div className="ov-daily-guidance">
        <section className="ov-daily-guidance-block">
          <div className="ov-daily-heading"><h3><Lightbulb size={14} aria-hidden="true" />今日建议</h3>{result && <button type="button" className="ov-daily-action" aria-label={`查看${result.title}结果`} onClick={() => onOpenDetail(taskDetail(result))}>查看结果<ArrowRight size={12} aria-hidden="true" /></button>}</div>
          <p>{advice}</p>
        </section>
        <section className="ov-daily-guidance-block ov-daily-attention">
          <div className="ov-daily-heading"><h3><CircleAlert size={14} aria-hidden="true" />重点关注</h3>{issue && <button type="button" className="ov-daily-action" onClick={() => onOpenDetail(taskDetail(issue))}>查看任务<ArrowRight size={12} aria-hidden="true" /></button>}</div>
          <p>{loading ? "正在核对任务状态…" : issue ? `${model.taskReady ? "当前有" : "可读取记录中有"} ${model.attention.length} 项需要关注的任务记录，建议查看状态和结果后再安排后续计算。` : !available ? "账号数据仅在登录后展示。" : model.taskReady ? "当前暂无需要关注的任务记录。" : "部分任务状态暂不可用，可刷新后重新查看。"}</p>
        </section>
        <section className="ov-daily-guidance-block ov-daily-plan">
          <div className="ov-daily-heading"><h3><CalendarCheck2 size={14} aria-hidden="true" />今日计划</h3><span>快捷操作</span></div>
          <div className="ov-daily-plan-actions">
            <button type="button" className="ov-daily-plan-entry" aria-label="继续结构设计" onClick={() => onNavigate("structureWorkbench")}><span className="ov-daily-plan-icon"><Atom size={14} aria-hidden="true" /></span><span className="ov-daily-plan-copy"><strong>继续结构设计</strong><small>绘制材料结构</small></span><ArrowRight size={13} aria-hidden="true" /></button>
            <button type="button" className="ov-daily-plan-entry ov-daily-plan-entry-knowledge" aria-label="补充文献线索" onClick={() => onNavigate("knowledge")}><span className="ov-daily-plan-icon"><BookOpen size={14} aria-hidden="true" /></span><span className="ov-daily-plan-copy"><strong>补充文献线索</strong><small>查找研究线索</small></span><ArrowRight size={13} aria-hidden="true" /></button>
          </div>
        </section>
      </div>
    </div>
  </div>;
}
