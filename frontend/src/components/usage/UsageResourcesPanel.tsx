import { useEffect, useState, type FormEvent } from "react";
import { BarChart3 } from "lucide-react";
import { useUsageRead } from "../../hooks/useUsageRead";
import { fetchUsageCatalog, fetchUsageRecords, fetchUsageSummary, UsageRequestError } from "../../services/usage";
import { USAGE_STATUSES, type UsageCatalog, type UsageMetric, type UsagePeriod, type UsageRecord, type UsageStatus, type UsageSummary } from "../../types/usage";
import { WorkbenchSelect } from "../structure-workbench/WorkbenchSelect";
import { formatMetric, metricLabel, recentBeijingDates, statusLabels, beijingPeriod, beijingTimestamp } from "./format";
import { UsageReadFeedback, usageErrorMessage } from "./UsageReadFeedback";
import { isUsageExplanation } from "./explanations";
import { USAGE_UNASSIGNED_NOTE, usageModuleGroupLabel, usageTaskTypeLabel } from "./moduleGroups";

const disabledError = (error: unknown) => error instanceof UsageRequestError && error.status === 503 && error.code === "usage_metering_disabled";
const number = (value: number) => value.toLocaleString("zh-CN");

export function UsageResourcesPanel({ refreshVersion, onBusyChange }: { refreshVersion: number; onBusyChange: (busy: boolean) => void }) {
  const [initial] = useState(() => recentBeijingDates());
  const [dates, setDates] = useState(initial);
  const [appliedDates, setAppliedDates] = useState(initial);
  const [preset, setPreset] = useState("30");
  const [module, setModule] = useState("");
  const [period, setPeriod] = useState<UsagePeriod>(() => beijingPeriod(initial.startDate, initial.endDate));
  const [validation, setValidation] = useState("");
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<UsageStatus | "">("");
  const [includeChildren, setIncludeChildren] = useState(false);
  const [checkVersion, setCheckVersion] = useState(0);
  const revision = refreshVersion + checkVersion;
  const catalog = useUsageRead("catalog", fetchUsageCatalog, revision);
  const enabled = catalog.data?.enabled === true;
  const summary = useUsageRead(JSON.stringify(period), signal => fetchUsageSummary(period, signal), revision, enabled);
  const query = { ...period, ...(status ? { status } : {}), page, page_size: 25, include_children: includeChildren };
  const records = useUsageRead(JSON.stringify(query), signal => fetchUsageRecords(query, signal), revision, enabled);
  const busy = catalog.loading || summary.loading || records.loading;
  useEffect(() => onBusyChange(busy), [busy, onBusyChange]);
  const unavailable = catalog.data?.enabled === false || [catalog.error, summary.error, records.error].some(disabledError);
  const dirty = dates.startDate !== appliedDates.startDate || dates.endDate !== appliedDates.endDate || module !== (period.module_group ?? "");

  function apply(event: FormEvent) {
    event.preventDefault();
    try {
      const next = { ...beijingPeriod(dates.startDate, dates.endDate), ...(module ? { module_group: module } : {}) };
      setPeriod(next); setAppliedDates({ ...dates }); setPage(1); setValidation("");
    } catch (error) { setValidation(usageErrorMessage(error)); }
  }

  return <>
    <div className="np-usage-section-header"><div><h2>资源观测</h2><p hidden className="np-usage-intro">资源观测用于诊断，分类成功次数以次数总览为准。按任务提交时间归属，统一使用北京时间（UTC+8）。</p></div></div>
    {unavailable ? <div className="np-usage-panel np-usage-empty" role="status"><BarChart3 aria-hidden="true" /><h2>用量统计尚未启用</h2><p hidden>启用后可在此查看已记录的个人用量。</p><button type="button" onClick={() => setCheckVersion(value => value + 1)}>重新检查</button></div> : <>
      <UsageReadFeedback {...catalog} hasData={Boolean(catalog.data)} label="统计目录" />
      {catalog.data && <>
        <form className="np-usage-panel np-usage-filters" onSubmit={apply} aria-label="用量筛选">
          <label>日期范围<select aria-label="快捷日期范围" value={preset} onChange={event => {
            setPreset(event.target.value);
            if (event.target.value) setDates(recentBeijingDates(Number(event.target.value)));
          }}><option value="7">近 7 天</option><option value="30">近 30 天</option><option value="90">近 90 天</option><option value="">自定义</option></select></label>
          <label>开始日期（北京时间）<input type="date" required value={dates.startDate} onChange={event => { setPreset(""); setDates(value => ({ ...value, startDate: event.target.value })); }} /></label>
          <label>结束日期（含当天）<input type="date" required value={dates.endDate} onChange={event => { setPreset(""); setDates(value => ({ ...value, endDate: event.target.value })); }} /></label>
          <div className="np-usage-field"><span id="usage-module-label">功能模块</span><WorkbenchSelect id="usage-module" ariaLabel="功能模块" value={module}
            options={[{ value: "", label: "全部模块" }, ...catalog.data.module_groups.map(item => ({ value: item.id, label: item.label }))]} onChange={setModule} constrainToScrollParent /></div>
          <button type="submit" className="np-usage-primary">应用筛选</button>
          <p className="np-usage-applied-filter">已应用：{appliedDates.startDate} 至 {appliedDates.endDate}（北京时间） · {catalog.data.module_groups.find(item => item.id === period.module_group)?.label ?? "全部模块"}{dirty && <span className="np-usage-pending" role="status">筛选尚未应用</span>}</p>
          <p hidden className="np-usage-filter-hint">最多 366 天；结束日期包含当天。仅记录启用后的任务；删除任务不会清除统计。</p>
          <p hidden className="np-usage-filter-hint">{USAGE_UNASSIGNED_NOTE}</p>
          {validation && <p role="alert" className="np-usage-filter-hint np-usage-error">{validation}</p>}
        </form>
        <div className="np-usage-tabpanel" aria-busy={summary.loading}>
          <UsageReadFeedback {...summary} hasData={Boolean(summary.data)} label="统计汇总" />
          {summary.data && <Summary data={summary.data} catalog={catalog.data} />}
        </div>
        <section className="np-usage-section" aria-labelledby="usage-records-heading" aria-busy={records.loading}>
          <div className="np-usage-section-header"><h2 id="usage-records-heading">用量明细</h2><div className="np-usage-record-filters">
            <div className="np-usage-field"><span>明细状态</span><WorkbenchSelect id="usage-status" ariaLabel="明细状态" value={status}
              options={[{ value: "", label: "全部状态" }, ...USAGE_STATUSES.map(value => ({ value, label: statusLabels[value] }))]}
              onChange={value => { setStatus(value as UsageStatus | ""); setPage(1); }} constrainToScrollParent /></div>
            <label className="np-usage-checkbox"><input type="checkbox" checked={includeChildren} onChange={event => { setIncludeChildren(event.target.checked); setPage(1); }} />显示子调用明细</label>
          </div></div>
          <p hidden className="np-usage-note">状态与子调用筛选仅影响明细。</p>
          <details hidden className="np-usage-disclosure"><summary>明细统计口径</summary><p className="np-usage-note">明细逐条展示历史任务尝试或子调用；旧失败尝试可与汇总中最新尝试的成功状态并存。根任务行仅显示自身记录的指标，不聚合子调用；子调用指标见子调用明细，汇总按统计口径去重。</p></details>
          <UsageReadFeedback {...records} hasData={Boolean(records.data)} label="用量明细" />
          {records.data && <RecordTable records={records.data.items} catalog={catalog.data} />}
          {(records.data || page > 1) &&
            <nav className="np-usage-pagination" aria-label="用量明细分页"><span>{records.data ? <>共 {number(records.data.total)} 条 · 第 {page} / {Math.max(1, Math.ceil(records.data.total / records.data.page_size))} 页</> : <>第 {page} 页</>}</span>
              <div><button type="button" disabled={records.loading || page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button><button type="button" disabled={records.loading || !records.data || page >= 10000 || page * records.data.page_size >= records.data.total} onClick={() => setPage(value => value + 1)}>下一页</button></div>
            </nav>}
          {page >= 10000 && <p className="np-usage-note">已达分页查询上限，请缩小日期范围或调整明细筛选。</p>}
        </section>
        <section hidden className="np-usage-section" aria-label="统计范围">
          <details className="np-usage-disclosure"><summary>统计范围</summary>
            <p className="np-usage-note">耗时为已观测时长，不等于 GPU 小时；产物体积为累计观测值，不代表当前存储占用。未采集的指标显示“未记录”。</p>
            <ul className="np-usage-coverage">{catalog.data.modules.map(item => <li key={item.id}><strong>{item.label}</strong><span className={`np-usage-coverage-${item.coverage}`}>{ { measured: "已计量", partial: "部分计量", external: "外部服务" }[item.coverage]}</span>{item.note && <p>{item.note}</p>}</li>)}</ul>
          </details>
        </section>
      </>}
    </>}
  </>;
}

function Summary({ data, catalog }: { data: UsageSummary; catalog: UsageCatalog }) {
  const daily = [...data.daily].sort((a, b) => a.date.localeCompare(b.date)).slice(-30);
  const max = Math.max(1, ...daily.map(day => day.tasks));
  const moduleLabel = (id: string) => catalog.modules.find(item => item.id === id)?.label ?? id;
  const primaryIds = ["execution_ms", "provider_total_tokens", "artifact_bytes"];
  const primary = primaryIds.flatMap(id => catalog.metrics.filter(metric => metric.id === id));
  const secondary = catalog.metrics.filter(metric => !primaryIds.includes(metric.id));
  return <>
    <section className="np-usage-kpis" aria-label="用量概览">{[
      ["任务", data.totals.tasks], ["任务尝试", data.totals.attempts], ["子调用", data.totals.child_calls]
    ].map(([label, value]) => <div className="np-usage-panel" key={String(label)}><span>{label}</span><strong>{number(Number(value))}</strong></div>)}</section>
    {data.totals.tasks === 0 && <p role="status" className="np-usage-note">所选范围内暂无已记录的任务。</p>}
    {(data.coverage.uncovered.length > 0 || data.coverage.warnings.length > 0) && <aside hidden={data.coverage.warnings.every(isUsageExplanation)} className="np-usage-coverage-warning" aria-label="统计覆盖提示">
      {data.coverage.uncovered.length > 0 && <p hidden>尚未覆盖：{data.coverage.uncovered.map(moduleLabel).join("、")}</p>}{data.coverage.warnings.map((warning, index) => <p hidden={isUsageExplanation(warning)} key={index}>{warning}</p>)}
    </aside>}
    <div className="np-usage-analysis-grid">
      <section className="np-usage-panel" aria-labelledby="usage-daily-heading"><h2 id="usage-daily-heading">每日任务数{data.daily.length > 30 ? "（范围内最近 30 个有记录日期）" : ""}</h2>
        <p hidden className="np-usage-note">按北京时间日期展示，最多显示最近 30 个有记录日期。</p>
        {daily.length ? <div className="np-usage-trend-scroll" role="region" aria-label="每日任务趋势，可横向滚动" tabIndex={0}><ol className="np-usage-trend">{daily.map(day => <li key={day.date} aria-label={`${day.date}，${day.tasks} 个任务`} title={`${day.date}：${day.tasks} 个任务`}>
          <span className="np-usage-bar-count">{number(day.tasks)}</span><span className="np-usage-bar-slot" aria-hidden="true"><span style={{ height: `${day.tasks / max * 100}%` }} /></span><small aria-hidden="true">{day.date.slice(5)}</small>
        </li>)}</ol></div> : <p className="np-usage-note">暂无每日记录。</p>}
      </section>
      <section className="np-usage-panel" aria-label="任务状态"><h2>任务状态</h2>
        <div className="np-usage-states">{USAGE_STATUSES.map(status => <span key={status} className={`np-usage-state np-usage-state-${status}`}>{statusLabels[status]} <strong>{number(data.totals.states[status] ?? 0)}</strong></span>)}</div>
        <details hidden className="np-usage-disclosure"><summary>任务统计口径</summary><p className="np-usage-note">任务数按逻辑根任务去重；状态取其最新一次任务尝试的最近观测状态。任务尝试数包含重试。</p></details>
      </section>
    </div>
    <section className="np-usage-section" aria-labelledby="usage-metrics-heading"><h2 id="usage-metrics-heading">已观测指标</h2>
      <MetricList metrics={primary} totals={data.totals.metrics} />
      {secondary.length > 0 && <details className="np-usage-disclosure"><summary>全部指标</summary><MetricList metrics={secondary} totals={data.totals.metrics} /></details>}
      <details hidden className="np-usage-disclosure"><summary>指标统计口径</summary><p className="np-usage-note">部分记录缺少指标时，显示已知部分的合计，并标明未记录数量。令牌显示“未记录”时，可能未采集或不适用于该任务，不代表 0。缓存输入和推理 Tokens 属于子集，不可与供应商总 Tokens 叠加。</p></details>
    </section>
    <section className="np-usage-section" aria-labelledby="usage-modules-heading"><h2 id="usage-modules-heading">按任务类型统计</h2><p hidden className="np-usage-note">同一模块内按具体任务类型展示。耗时仅合计已记录的部分；根任务历时包含等待，不等于执行耗时。</p>
      {data.modules.length ? <div className="np-usage-table-scroll" role="region" aria-label="按任务类型用量表，可横向滚动" tabIndex={0}><table><thead><tr><th>模块 / 任务类型</th><th className="np-usage-numeric">任务</th><th className="np-usage-numeric">任务尝试</th><th className="np-usage-numeric">子调用</th><th className="np-usage-numeric">已观测执行耗时（已知合计）</th><th className="np-usage-numeric">根任务历时（已知合计）</th></tr></thead><tbody>{data.modules.map(item => <tr key={item.module}>
        <th scope="row">{usageModuleGroupLabel(item.module, catalog.module_groups)}<small className="np-usage-task-type">{usageTaskTypeLabel(item.module, catalog.modules)}</small></th><td className="np-usage-numeric">{number(item.tasks)}</td><td className="np-usage-numeric">{number(item.attempts)}</td><td className="np-usage-numeric">{number(item.child_calls)}</td><MetricTotalCell id="execution_ms" amount={item.metrics.execution_ms} /><MetricTotalCell id="observed_wall_ms" amount={item.metrics.observed_wall_ms} />
      </tr>)}</tbody></table></div> : <p className="np-usage-note">暂无任务类型统计。</p>}
    </section>
  </>;
}

function MetricList({ metrics, totals }: { metrics: UsageCatalog["metrics"]; totals: Record<string, UsageMetric> }) {
  return <dl className="np-usage-metrics">{metrics.map(metric => {
    const amount = totals[metric.id];
    return <div key={metric.id}><dt>{metricLabel(metric)}</dt><dd>{formatMetric(metric.id, amount?.value, metric.unit)}</dd>{amount && <small>{number(amount.known_count)} 条已记录 · {number(amount.unknown_count)} 条未记录</small>}</div>;
  })}</dl>;
}

function MetricTotalCell({ id, amount }: { id: string; amount: UsageMetric | undefined }) {
  return <td className="np-usage-numeric"><div>{formatMetric(id, amount?.value)}</div>{amount && <small>{number(amount.known_count)} 条已记录 · {number(amount.unknown_count)} 条未记录</small>}</td>;
}

function RecordTable({ records, catalog }: { records: UsageRecord[]; catalog: UsageCatalog }) {
  if (!records.length) return <p className="np-usage-note">所选条件下暂无用量明细。</p>;
  return <div className="np-usage-table-scroll" role="region" aria-label="个人用量明细表，可横向滚动" tabIndex={0}><table><thead><tr><th>提交时间（北京时间）</th><th>模块 / 任务类型</th><th>状态</th><th className="np-usage-numeric">执行耗时</th><th className="np-usage-numeric">总令牌</th><th className="np-usage-numeric">累计已观测产物体积</th></tr></thead><tbody>{records.map(record => <tr key={`${record.module}:${record.task_id}:${record.attempt_id}:${record.kind}`}>
    <td><time dateTime={record.submitted_at}>{beijingTimestamp(record.submitted_at)}</time></td>
    <td><strong>{usageModuleGroupLabel(record.module, catalog.module_groups)}</strong><small className="np-usage-task-type">{usageTaskTypeLabel(record.module, catalog.modules)}</small><details><summary>任务详情</summary><dl className="np-usage-record-detail">
      {[['任务', record.task_id], ['尝试', record.attempt_id], ['类型', record.kind], ['来源', record.source], ['父任务', record.parent_task_id], ['根任务', record.root_task_id], ['开始（北京时间）', beijingTimestamp(record.started_at)], ['结束（北京时间）', beijingTimestamp(record.finished_at)]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? "—"}</dd></div>)}
      {catalog.metrics.map(metric => <div key={metric.id}><dt>{metricLabel(metric)}</dt><dd>{formatMetric(metric.id, record.metrics[metric.id], metric.unit)}</dd></div>)}
    </dl></details>{record.parent_task_id && <small>子调用</small>}</td>
    <td><span className={`np-usage-state np-usage-state-${record.status}`}>{statusLabels[record.status] ?? record.status}</span></td>
    <td className="np-usage-numeric">{formatMetric("execution_ms", record.metrics.execution_ms)}</td><td className="np-usage-numeric">{formatMetric("provider_total_tokens", record.metrics.provider_total_tokens)}</td><td className="np-usage-numeric">{formatMetric("artifact_bytes", record.metrics.artifact_bytes)}</td>
  </tr>)}</tbody></table></div>;
}
