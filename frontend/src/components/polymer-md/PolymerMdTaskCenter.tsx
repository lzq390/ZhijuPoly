import { Activity, ArrowRight, Atom, Ban, ChevronLeft, ChevronRight, ClipboardList, LoaderCircle, LockKeyhole, RefreshCw, TriangleAlert } from "lucide-react";
import type { PolymerMdChainLength, PolymerMdJob, PolymerMdJobPage } from "../../types/polymerMd";
import { QUALITY_LABELS, stageLabel, STATUS_LABELS, terminalJob } from "./config";
import { PolymerMdJobTiming, polymerMdTimingIsLive, usePolymerMdClock } from "./PolymerMdJobTiming";
import { MODEL_LABELS } from "./PolymerMdModelFields";
import { requestedCalculationDescription } from "./presentation";
import { PolymerMdNumericText } from "./PolymerMdNumericText";

type Props = {
  history: PolymerMdJobPage | null;
  page: number;
  error: string | null;
  busy: string | null;
  guest: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onCancel: (id: string) => void;
  onPageChange: (page: number) => void;
  onRefresh: () => void;
  onLogin: () => void;
};
function lengthText(length: PolymerMdChainLength | undefined, alternating = false) {
  if (!length) return null;
  const value = length.value.toLocaleString("zh-CN");
  return length.mode === "atoms" ? `每链目标 ${value} 个原子`
    : length.mode === "molecular_weight" ? `每链目标分子量 ${value} g/mol`
    : `每链目标${alternating ? "交替周期" : "聚合度 DP"} ${value}`;
}
function TaskRow({ job, now, selected, busy, onSelect, onCancel }: {
  job: PolymerMdJob; now: number; selected: boolean; busy: boolean;
  onSelect: () => void; onCancel: () => void;
}) {
  const request = job.scientific_request;
  const model = request.model_configuration;
  const components = model?.components ?? [{ id: "", smiles: request.smiles }];
  const sizing = model ? lengthText(model.chain_length, model.mode === "alternating")
    : `每链目标 ${request.n_atom.toLocaleString("zh-CN")} 个原子`;
  return <li className={`np-pmd-task-row${selected ? " is-selected" : ""}`} data-status={job.status} aria-label={`任务 ${job.job_id}`}>
    <header className="np-pmd-task-row-header"><div className="np-pmd-task-heading"><span className="np-pmd-task-model-icon"><Atom aria-hidden="true" /></span><div><h4>{model ? MODEL_LABELS[model.mode] : "聚合物 MD 计算"}</h4><p className="np-pmd-task-id"><span>任务 ID</span><code className="np-pmd-code">{job.job_id}</code></p></div></div><span className={`np-pmd-status-pill is-${job.status}`}>{STATUS_LABELS[job.status]}</span></header>
    <div className="np-pmd-task-conditions"><span><small>温度</small><strong><span className="np-pmd-numeric-value">{request.temp}</span> K</strong></span><span><small>压力</small><strong><span className="np-pmd-numeric-value">{request.pressure}</span> atm</strong></span><span><small>力场</small><strong><span className="np-pmd-task-technical">{request.forcefield}</span></strong></span><span><small>链数</small><strong>请求 <span className="np-pmd-numeric-value">{request.n_chain}</span> 条链</strong></span>{sizing ? <span><small>链长</small><strong><PolymerMdNumericText>{sizing}</PolymerMdNumericText></strong></span> : null}</div>
    <div className="np-pmd-task-components">{components.map((component, index) => <div key={component.id || index}>
      <span>{component.id ? `组分 ${component.id}` : "重复单元"}</span><code tabIndex={0} title={component.smiles} aria-label={`${component.id ? `组分 ${component.id}` : "重复单元"} SMILES`}>{component.smiles}</code>
      {model?.mode === "blend" ? <small><PolymerMdNumericText>{`请求 ${component.n_chain ?? ""} 条链 · ${lengthText(component.chain_length) ?? ""}`}</PolymerMdNumericText></small>
        : model?.mode === "random" && model.ratios?.[index] !== undefined ? <small><PolymerMdNumericText>{`请求重复单元数比例 ${(model.ratios[index] * 100).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`}</PolymerMdNumericText></small> : null}
    </div>)}</div>
    {model?.mode === "block" && model.blocks?.length ? <p className="np-pmd-task-scope"><PolymerMdNumericText>{`目标嵌段：${model.blocks.map(block => `${block.component_id}×${block.dp}`).join(" → ")}`}</PolymerMdNumericText></p> : null}
    <p className="np-pmd-task-scope np-pmd-task-calculations"><span>计算范围</span><span>{requestedCalculationDescription(request)}</span></p>
    <div className="np-pmd-task-stage"><span>当前阶段</span><strong className="np-pmd-task-stage-chip"><Activity aria-hidden="true" />{stageLabel(job.stage)}</strong><span>科学质量</span><span className={`np-pmd-task-quality is-${job.quality_status}`}>{QUALITY_LABELS[job.quality_status]}</span></div>
    <footer className="np-pmd-task-row-footer"><div className="np-pmd-task-time-panel"><PolymerMdJobTiming job={job} now={now} /></div><div className="np-pmd-task-actions">
      <button type="button" className="is-primary" onClick={onSelect}>查看任务<ArrowRight aria-hidden="true" /></button>
      {!terminalJob(job.status) ? <button type="button" className="np-pmd-task-cancel" disabled={busy || job.status === "cancel_requested"} onClick={onCancel}><Ban aria-hidden="true" />{job.status === "cancel_requested" ? "正在取消" : "取消"}</button> : null}
    </div></footer>
  </li>;
}

export function PolymerMdTaskCenter({ history, page, error, busy, guest, selectedId, onSelect, onCancel, onPageChange, onRefresh, onLogin }: Props) {
  const current = !guest && history?.page === page ? history : null;
  const loading = !guest && !current && !error;
  const now = usePolymerMdClock(Boolean(current?.items.some(polymerMdTimingIsLive)));
  const pages = current ? Math.max(1, Math.ceil(current.total / current.page_size)) : null;
  return <section className="np-pmd-task-center" aria-labelledby="polymer-md-task-center-title">
    <header className="np-pmd-section-heading"><div><h3 id="polymer-md-task-center-title">我的计算任务</h3><p>查看正式任务的执行状态、实际运行时间与历史结果。</p></div>{!guest && history ? <span className="np-pmd-task-count">共 {history.total} 个任务</span> : null}</header>
    {guest ? <div className="np-pmd-empty-state"><LockKeyhole aria-hidden="true" /><h3>登录后查看计算任务</h3><p>登录后可查看本人的正式任务与历史结果。</p><button type="button" onClick={onLogin}>登录账号</button></div> : <>
      {error ? <div className="np-pmd-task-error" role="alert"><TriangleAlert aria-hidden="true" /><div><strong>{current ? "任务列表刷新失败" : "任务列表暂不可读取"}</strong><p>{error}{current ? " 当前保留上次读取的任务。" : ""}</p></div><button type="button" onClick={onRefresh}><RefreshCw aria-hidden="true" />重试读取任务</button></div> : null}
      {loading ? <div className="np-pmd-empty-state" role="status"><LoaderCircle className="np-sw-spin" aria-hidden="true" /><h3>正在读取第 {page} 页任务…</h3><p>正在读取本人的正式任务与执行状态。</p></div>
        : current?.items.length ? <ul className="np-pmd-task-list">{current.items.map(job => <TaskRow key={job.job_id} job={job} now={now} selected={selectedId === job.job_id} busy={Boolean(busy)} onSelect={() => onSelect(job.job_id)} onCancel={() => onCancel(job.job_id)} />)}</ul>
        : !error ? <div className="np-pmd-empty-state"><ClipboardList aria-hidden="true" /><h3>暂无任务记录。</h3><p>提交正式计算后，可在这里查看任务状态与历史。</p></div> : null}
      <nav className="np-pmd-pagination" aria-label="任务分页"><button type="button" disabled={loading || page <= 1} onClick={() => onPageChange(page - 1)}><ChevronLeft aria-hidden="true" />上一页</button><span>{loading ? `正在读取第 ${page} 页` : current ? `第 ${page} 页 / 共 ${pages} 页` : `第 ${page} 页读取失败`}</span><button type="button" disabled={loading || !current || page >= (pages ?? page)} onClick={() => onPageChange(page + 1)}>下一页<ChevronRight aria-hidden="true" /></button></nav>
    </>}
  </section>;
}
