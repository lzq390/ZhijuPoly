import { useEffect, useState, type FormEvent } from "react";
import { BarChart3, RefreshCw } from "lucide-react";
import { useAuth } from "../auth/AuthProvider";
import { requestServiceAccess } from "../auth/guestAccess";
import { fetchUsageCatalog, fetchUsageRecords, fetchUsageSummary, UsageRequestError } from "../services/usage";
import { USAGE_STATUSES, type UsageCatalog, type UsagePeriod, type UsageRecord, type UsageRecords, type UsageStatus, type UsageSummary } from "../types/usage";
import { ModulePageHeader } from "./ModulePageHeader";
import { formatMetric, metricLabel, recentUtcDates, statusLabels, utcPeriod, utcTimestamp } from "./usage/format";
import "./PersonalUsagePage.css";

type Load<T> = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; data: T };
const loading = { state: "loading" } as const;
const disabledError = (error: unknown) => error instanceof UsageRequestError && error.status === 503 && error.code === "usage_metering_disabled";
const errorMessage = (error: unknown) => error instanceof Error ? error.message : "暂时无法读取用量，请重试。";
const number = (n: number) => n.toLocaleString("zh-CN");

export function PersonalUsagePage() {
  const auth = useAuth();
  if (auth?.status !== "authenticated") return <section className="np-module-page np-usage-page">
    <ModulePageHeader>我的用量</ModulePageHeader>
    <div className="np-module-page-body np-usage-body"><div className="np-usage-panel np-usage-empty">
      <BarChart3 aria-hidden="true" /><h2>登录后查看我的用量</h2><p>这里仅展示当前账号自己的统计记录。</p>
      <button type="button" onClick={() => auth ? auth.login() : requestServiceAccess()}>登录账号</button>
    </div></div>
  </section>;
  // Session identity owns every filter/result. A different account/session remounts
  // this subtree; privateFetch additionally fences in-flight response bodies.
  return <SignedInUsage key={`${auth.session.user!.id}:${auth.session.session_id}`} />;
}

function SignedInUsage() {
  const [initial] = useState(() => recentUtcDates());
  const [dates, setDates] = useState(initial);
  const [preset, setPreset] = useState("30");
  const [module, setModule] = useState("");
  const [period, setPeriod] = useState<UsagePeriod>(() => utcPeriod(initial.startDate, initial.endDate));
  const [validation, setValidation] = useState("");
  const [catalog, setCatalog] = useState<Load<UsageCatalog>>(loading);
  const [summary, setSummary] = useState<Load<UsageSummary>>(loading);
  const [records, setRecords] = useState<Load<UsageRecords>>(loading);
  const [disabled, setDisabled] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<UsageStatus | "">("");
  const [includeChildren, setIncludeChildren] = useState(false);
  const readyCatalog = catalog.state === "ready" ? catalog.data : null;
  const enabled = readyCatalog?.enabled === true;

  useEffect(() => {
    const controller = new AbortController(); let active = true;
    setCatalog(loading); setSummary(loading); setRecords(loading); setDisabled(false);
    fetchUsageCatalog(controller.signal).then(data => { if (active) setCatalog({ state: "ready", data }); })
      .catch(error => { if (active && !controller.signal.aborted) {
        if (disabledError(error)) setDisabled(true);
        else setCatalog({ state: "error", message: errorMessage(error) });
      } });
    return () => { active = false; controller.abort(); };
  }, [refresh]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController(); let active = true;
    setSummary(loading);
    fetchUsageSummary(period, controller.signal).then(data => { if (active) setSummary({ state: "ready", data }); })
      .catch(error => { if (active && !controller.signal.aborted) {
        if (disabledError(error)) setDisabled(true);
        else setSummary({ state: "error", message: errorMessage(error) });
      } });
    return () => { active = false; controller.abort(); };
  }, [enabled, readyCatalog, period]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController(); let active = true;
    setRecords(loading);
    fetchUsageRecords({ ...period, ...(status ? { status } : {}), page, page_size: 25, include_children: includeChildren }, controller.signal)
      .then(data => { if (active) setRecords({ state: "ready", data }); })
      .catch(error => { if (active && !controller.signal.aborted) {
        if (disabledError(error)) setDisabled(true);
        else setRecords({ state: "error", message: errorMessage(error) });
      } });
    return () => { active = false; controller.abort(); };
  }, [enabled, readyCatalog, period, page, status, includeChildren]);

  const retry = () => setRefresh(value => value + 1);
  function apply(event: FormEvent) {
    event.preventDefault();
    try { const next = utcPeriod(dates.startDate, dates.endDate, module); setPeriod(next); setPage(1); setValidation(""); }
    catch (error) { setValidation(errorMessage(error)); }
  }
  const unavailable = disabled || (readyCatalog !== null && !readyCatalog.enabled);
  return <section className="np-module-page np-usage-page">
    <ModulePageHeader actions={<button type="button" className="np-usage-refresh" onClick={retry} aria-label="刷新用量"><RefreshCw aria-hidden="true" /><span>刷新</span></button>}>我的用量</ModulePageHeader>
    <div className="np-module-page-body np-usage-body">
      <p className="np-usage-intro">当前账号的活动统计。按任务提交时间归属，统一使用 UTC。仅作统计，不扣费、不限制额度。</p>
      {unavailable ? <div className="np-usage-panel np-usage-empty" role="status"><BarChart3 aria-hidden="true" /><h2>用量统计尚未启用</h2><p>启用后可在此查看已记录的个人用量。</p><button type="button" onClick={retry}>重新检查</button></div>
        : catalog.state === "error" ? <ErrorPanel message={catalog.message} retry={retry} />
        : catalog.state === "loading" ? <p role="status" className="np-usage-panel">正在加载用量统计…</p>
        : <>
          <form className="np-usage-panel np-usage-filters" onSubmit={apply} aria-label="用量筛选">
            <label>日期范围<select aria-label="快捷日期范围" value={preset} onChange={event => {
              setPreset(event.target.value);
              if (event.target.value) setDates(recentUtcDates(Number(event.target.value)));
            }}><option value="7">近 7 天</option><option value="30">近 30 天</option><option value="90">近 90 天</option><option value="">自定义</option></select></label>
            <label>开始日期（UTC）<input type="date" required value={dates.startDate} onChange={event => { setPreset(""); setDates(value => ({ ...value, startDate: event.target.value })); }} /></label>
            <label>结束日期（含当天）<input type="date" required value={dates.endDate} onChange={event => { setPreset(""); setDates(value => ({ ...value, endDate: event.target.value })); }} /></label>
            <label>功能<select aria-label="功能" value={module} onChange={event => setModule(event.target.value)}><option value="">全部功能</option>{catalog.data.modules.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
            <button type="submit" className="np-usage-primary">应用筛选</button>
            <p className="np-usage-filter-hint">最多 366 天；结束日期包含当天。仅记录启用后的任务；删除任务不会清除统计。</p>
            {validation && <p role="alert" className="np-usage-filter-hint np-usage-error">{validation}</p>}
          </form>
          {summary.state === "loading" ? <p role="status" className="np-usage-panel">正在读取统计汇总…</p>
            : summary.state === "error" ? <ErrorPanel message={summary.message} retry={retry} />
            : <Summary data={summary.data} catalog={catalog.data} />}
          <section className="np-usage-panel" aria-labelledby="usage-records-heading">
            <div className="np-usage-section-header"><h2 id="usage-records-heading">用量明细</h2><div className="np-usage-record-filters">
              <label>明细状态<select aria-label="明细状态" value={status} onChange={event => { setStatus(event.target.value as UsageStatus | ""); setPage(1); }}><option value="">全部状态</option>{USAGE_STATUSES.map(value => <option key={value} value={value}>{statusLabels[value]}</option>)}</select></label>
              <label className="np-usage-checkbox"><input type="checkbox" checked={includeChildren} onChange={event => { setIncludeChildren(event.target.checked); setPage(1); }} />显示子调用明细</label>
            </div></div>
            <p className="np-usage-note">状态与子调用筛选仅影响明细。汇总中的根任务不会重复累计子调用令牌。</p>
            {records.state === "loading" ? <p role="status">正在读取用量明细…</p>
              : records.state === "error" ? <ErrorPanel message={records.message} retry={retry} />
              : <><RecordTable records={records.data.items} catalog={catalog.data} />
                <nav className="np-usage-pagination" aria-label="用量明细分页"><span>共 {number(records.data.total)} 条 · 第 {records.data.page} / {Math.max(1, Math.ceil(records.data.total / records.data.page_size))} 页</span>
                  <div><button type="button" disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button><button type="button" disabled={page * records.data.page_size >= records.data.total} onClick={() => setPage(value => value + 1)}>下一页</button></div>
                </nav></>}
          </section>
          <section className="np-usage-panel" aria-labelledby="usage-coverage-heading"><h2 id="usage-coverage-heading">统计范围</h2><p className="np-usage-note">耗时为已观测时长，不等于 GPU 小时；产物体积为累计观测值，不代表当前存储占用。未采集的指标显示“未记录”。</p>
            <ul className="np-usage-coverage">{catalog.data.modules.map(item => <li key={item.id}><strong>{item.label}</strong><span className={`np-usage-coverage-${item.coverage}`}>{ { measured: "已计量", partial: "部分计量", external: "外部服务" }[item.coverage]}</span>{item.note && <p>{item.note}</p>}</li>)}</ul>
          </section>
        </>}
    </div>
  </section>;
}

function ErrorPanel({ message, retry }: { message: string; retry: () => void }) {
  return <div className="np-usage-panel np-usage-error"><p role="alert">{message}</p><button type="button" onClick={retry}>重试</button></div>;
}
function Summary({ data, catalog }: { data: UsageSummary; catalog: UsageCatalog }) {
  const daily = [...data.daily].sort((a, b) => a.date.localeCompare(b.date)).slice(-30);
  const max = Math.max(1, ...daily.map(day => day.tasks));
  const moduleLabel = (id: string) => catalog.modules.find(item => item.id === id)?.label ?? id;
  return <>
    <section className="np-usage-kpis" aria-label="用量概览">{[
      ["任务", data.totals.tasks], ["执行尝试", data.totals.attempts], ["子调用", data.totals.child_calls]
    ].map(([label, value]) => <div className="np-usage-panel" key={String(label)}><span>{label}</span><strong>{number(Number(value))}</strong></div>)}</section>
    {data.totals.tasks === 0 && <p role="status" className="np-usage-panel">所选范围内暂无已记录的任务。</p>}
    <section className="np-usage-panel" aria-label="任务状态"><h2>任务状态</h2><div className="np-usage-states">{USAGE_STATUSES.map(status => <span key={status} className={`np-usage-state np-usage-state-${status}`}>{statusLabels[status]} <strong>{number(data.totals.states[status] ?? 0)}</strong></span>)}</div></section>
    <section className="np-usage-panel" aria-labelledby="usage-metrics-heading"><h2 id="usage-metrics-heading">已观测指标</h2><p className="np-usage-note">部分记录缺少指标时，显示已知部分的合计，并标明未记录数量。</p><dl className="np-usage-metrics">{catalog.metrics.map(metric => {
      const amount = data.totals.metrics[metric.id];
      return <div key={metric.id}><dt>{metricLabel(metric)}</dt><dd>{formatMetric(metric.id, amount?.value, metric.unit)}</dd>{amount && <small>{number(amount.known_count)} 条已记录 · {number(amount.unknown_count)} 条未记录</small>}</div>;
    })}</dl></section>
    <section className="np-usage-panel" aria-labelledby="usage-modules-heading"><h2 id="usage-modules-heading">按功能统计</h2>{data.modules.length ? <div className="np-usage-table-scroll" role="region" aria-label="按功能用量表，可横向滚动" tabIndex={0}><table><thead><tr><th>功能</th><th>任务</th><th>执行尝试</th><th>子调用</th><th>执行耗时</th></tr></thead><tbody>{data.modules.map(item => <tr key={item.module}><th scope="row">{item.label}</th><td>{number(item.tasks)}</td><td>{number(item.attempts)}</td><td>{number(item.child_calls)}</td><td>{formatMetric("execution_ms", item.metrics.execution_ms?.value)}</td></tr>)}</tbody></table></div> : <p className="np-usage-note">暂无功能统计。</p>}</section>
    <section className="np-usage-panel" aria-labelledby="usage-daily-heading"><h2 id="usage-daily-heading">每日任务数{data.daily.length > 30 ? "（展示范围内最近 30 日）" : ""}</h2>{daily.length ? <div className="np-usage-trend-scroll" role="region" aria-label="每日任务趋势，可横向滚动" tabIndex={0}><ol className="np-usage-trend">{daily.map(day => <li key={day.date} aria-label={`${day.date}，${day.tasks} 个任务`} title={`${day.date}：${day.tasks} 个任务`}><span className="np-usage-bar-count">{number(day.tasks)}</span><span className="np-usage-bar-slot" aria-hidden="true"><span style={{ height: `${day.tasks / max * 100}%` }} /></span><small aria-hidden="true">{day.date.slice(5)}</small></li>)}</ol></div> : <p className="np-usage-note">暂无每日记录。</p>}</section>
    {(data.coverage.uncovered.length > 0 || data.coverage.warnings.length > 0) && <aside className="np-usage-panel np-usage-coverage-warning" aria-label="统计覆盖提示">{data.coverage.uncovered.length > 0 && <p>尚未覆盖：{data.coverage.uncovered.map(moduleLabel).join("、")}</p>}{data.coverage.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</aside>}
  </>;
}

function RecordTable({ records, catalog }: { records: UsageRecord[]; catalog: UsageCatalog }) {
  if (!records.length) return <p className="np-usage-note">所选条件下暂无用量明细。</p>;
  return <div className="np-usage-table-scroll" role="region" aria-label="个人用量明细表，可横向滚动" tabIndex={0}><table><thead><tr><th>提交时间（UTC）</th><th>功能 / 任务</th><th>状态</th><th>执行耗时</th><th>总令牌</th><th>累计已观测产物体积</th></tr></thead><tbody>{records.map(record => <tr key={`${record.module}:${record.task_id}:${record.attempt_id}:${record.kind}`}>
    <td><time dateTime={record.submitted_at}>{utcTimestamp(record.submitted_at)}</time></td>
    <td><strong>{catalog.modules.find(item => item.id === record.module)?.label ?? record.module}</strong><details><summary>任务详情</summary><dl className="np-usage-record-detail">
      {[['任务', record.task_id], ['尝试', record.attempt_id], ['类型', record.kind], ['来源', record.source], ['父任务', record.parent_task_id], ['根任务', record.root_task_id], ['开始（UTC）', utcTimestamp(record.started_at)], ['结束（UTC）', utcTimestamp(record.finished_at)]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? "—"}</dd></div>)}
      {catalog.metrics.map(metric => <div key={metric.id}><dt>{metricLabel(metric)}</dt><dd>{formatMetric(metric.id, record.metrics[metric.id], metric.unit)}</dd></div>)}
    </dl></details>{record.parent_task_id && <small>子调用</small>}</td>
    <td><span className={`np-usage-state np-usage-state-${record.status}`}>{statusLabels[record.status] ?? record.status}</span></td>
    <td>{formatMetric("execution_ms", record.metrics.execution_ms)}</td><td>{formatMetric("provider_total_tokens", record.metrics.provider_total_tokens)}</td><td>{formatMetric("artifact_bytes", record.metrics.artifact_bytes)}</td>
  </tr>)}</tbody></table></div>;
}
