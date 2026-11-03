import { useEffect, useRef, useState } from "react";
import { useAuth } from "../../auth/AuthProvider";
import { useUsageRead } from "../../hooks/useUsageRead";
import { fetchEntitlements, fetchUsageOperationsByGroup } from "../../services/entitlements";
import { fetchUsageActivityByGroup } from "../../services/usage";
import { UsageActivityPanel } from "./UsageActivityPanel";
import type { SpecificationField } from "../../types/entitlements";
import type { UsageModuleGroup } from "../../types/usage";
import { WorkbenchSelect } from "../structure-workbench/WorkbenchSelect";
import { UsageOperationDrawer, operationStateLabels } from "./UsageOperationDrawer";
import { UsageReadFeedback } from "./UsageReadFeedback";
import { beijingTimestamp } from "./format";
import { shortUsageTaskId, usageModuleGroupLabel, usageTaskTypeLabel, USAGE_UNASSIGNED_NOTE } from "./moduleGroups";

const modules: Record<string,string> = { assistant:"通用助手",tg_assistant:"Tg 助手",recording_summary:"浏览记录摘要",knowledge:"知识检索",online_knowledge:"在线知识抽取",conditional_generation:"Tg 条件生成",polytao:"PolyTAO 生成",predict:"性能预测",ocsr:"分子图像识别",retrosynthesis:"单体逆合成",reverse_design:"逆向设计",query:"相似性查询",structure:"结构处理",database_browser:"历史数据库操作（未细分）",database_query:"数据库查询",database_filter:"数据库筛选",database_analysis:"数据库分析",dft_history:"DFT 历史查询",monomer_md:"分子动力学",monomer_dft:"DFT 计算",polymer_md:"聚合物分子动力学",polymerization:"单体聚合",polymerization_batch:"批量聚合",batch_preflight:"导入校验",md_demo:"分子动力学演示",openscience:"OpenScience 独立服务" };
const fields: Record<string,string> = { property_count:"性质数量",image_bytes:"图像字节",image_pixels:"图像像素",num_beams:"搜索束数",num_return_sequences:"返回候选数",max_new_tokens:"最大生成长度",max_results:"最多结果数",monomer_count:"单体数量",operation:"操作类型",atom_count:"原子数",candidate_count:"目标候选数",top_k:"top-k",top_p:"top-p",temperature:"温度",delta_tg:"目标Tg变化",max_length:"最大长度",message_count:"消息数",input_characters:"输入字符数",image_count:"图像数",max_output_tokens:"最大输出token",provider_attempt_limit:"供应商尝试上限",evidence_characters:"证据字符数",run_mode:"运行模式",protocol:"协议",requested_steps:"请求步数",requested_atom_count:"请求原子数",calculation_type:"计算类型",model:"模型",total_atoms:"总原子数",requested_properties:"请求性质",max_steps:"优化步数",conformer_max_iterations:"构象迭代上限",raw_pairs:"原始组合数",valid_pairs:"有效组合数",rows_a:"A表行数",rows_b:"B表行数",max_papers:"文献上限",mode:"模式",candidate_size:"候选库大小",match_mode:"匹配模式",similarity_threshold:"相似性阈值" };
const ruleLabels: Record<string,string> = { generation_partial_counts:"生成部分有效候选",chemistry_no_match_counts:"化学服务明确无匹配",batch_partial_counts:"批量部分完成",online_partial_counts:"在线抽取部分完成",dft_non_converged_counts:"DFT优化未收敛但完整交付",md_demo_counts:"MD演示" };
function specification(field: SpecificationField) {
  if (field.values) return field.values.join(" / ") + (field.type === "enum_list" ? `（选 ${field.min_items}–${field.max_items} 项）` : "");
  return `${field.exclusive_min ? "> " : ""}${field.min}–${field.max}`;
}
type Props = {
  activeTab?: "overview" | "operations" | "resources";
  refreshVersion?: number;
  onBusyChange?: (busy: boolean) => void;
};

export function PersonalEntitlementsPanel({ activeTab = "overview", refreshVersion = 0, onBusyChange }: Props) {
  const auth = useAuth();
  const [source, setSource] = useState<"activity" | "receipts">("activity");
  const [activityOffset, setActivityOffset] = useState(0);
  const [activityModuleGroup, setActivityModuleGroup] = useState("");
  const [offset, setOffset] = useState(0);
  const [moduleGroup, setModuleGroup] = useState("");
  const [savedOperationGroups, setSavedOperationGroups] = useState<UsageModuleGroup[]>([]);
  const [selected, setSelected] = useState<{ id: string; readResult: boolean; requestKey: number } | null>(null);
  const requestSequence = useRef(0);
  const trigger = useRef<HTMLElement | null>(null);
  const balance = useUsageRead("entitlements", fetchEntitlements, refreshVersion);
  // Durable receipts remain readable when new allowance admission is disabled.
  const operations = useUsageRead(`operations:group:${moduleGroup}:${offset}`,
    signal => fetchUsageOperationsByGroup(offset, moduleGroup || undefined, signal), refreshVersion);
  const activity = useUsageRead(`activity:group:${activityModuleGroup}:${activityOffset}`,
    signal => fetchUsageActivityByGroup(activityOffset, activityModuleGroup || undefined, signal), refreshVersion);
  useEffect(() => { if (operations.data) setSavedOperationGroups(operations.data.module_groups ?? []); }, [operations.data]);
  const operationGroups = operations.data?.module_groups ?? savedOperationGroups;
  const data = balance.data, policy = data?.policy;
  const busy = balance.loading || operations.loading || activity.loading;
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  const receiptsActive = activeTab === "operations" && source === "receipts";
  useEffect(() => { if (!receiptsActive) setSelected(null); }, [receiptsActive]);

  return <>
    <div id="usage-panel-overview" role="tabpanel" aria-labelledby="usage-tab-overview" hidden={activeTab !== "overview"} className="np-usage-tabpanel">
      <section className="np-usage-section" aria-labelledby="usage-pools-heading" aria-busy={balance.loading}>
        <div className="np-usage-section-header"><h2 id="usage-pools-heading">分类成功次数</h2><div className="np-usage-account-meta"><span className="np-usage-state">{auth?.session.account_type === "free" ? "免费账号" : "会员账号"}</span><span hidden>次数余额不改变功能权限</span></div></div>
        <UsageReadFeedback loading={balance.loading} error={balance.error} hasData={Boolean(data)} label="分类次数" retry={balance.retry} />
        {data && !data.enabled ? <p role="status">分类次数尚未启用。<span hidden>已接入任务可在“操作记录”查看，历史计次回执仍可读取。</span></p> : data && policy && <>
          <p className="np-usage-note">当前{({ day: "日", week: "周", month: "月" })[policy.period]}周期（北京时间）：{beijingTimestamp(data.period_start)} 至 {beijingTimestamp(data.period_end)}<span hidden>，结束时刻不包含在内。按操作获准预留时归属周期。</span></p>
          {policy.example && <p hidden className="np-usage-coverage-warning" role="note">当前为示例策略，不代表正式套餐承诺。</p>}
          <p hidden className="np-usage-note">成功交付计次；处理中先预留。待确认次数包含在预留中，不能再次相加。读取回执或已有结果不重新执行、不新增成功次数。</p>
          {Boolean(data.held_outside_current_period) && <p className="np-usage-coverage-warning" role="note">另有 {data.held_outside_current_period} 次旧周期预留尚未结束，跨周期待确认 {data.unknown_across_periods ?? "未记录"} 次。<span hidden>它们仍参与运行保护，不应当作已经释放。</span></p>}
          {data.admission_blocked_by_period && <p role="alert" className="np-usage-coverage-warning">存在尚未结束的冲突周期，当前余额不代表可以提交；请先确认旧操作或等待原周期结束。</p>}
          <div className="np-usage-pool-grid">{data.pools.map(pool => {
            const label = policy.pool_labels[pool.pool_id] ?? pool.pool_id;
            const limit = pool.limit;
            const measured = limit !== null && Number.isFinite(limit) && limit > 0;
            const used = measured ? Math.min(limit, Math.max(0, pool.success_used)) : 0;
            const reserved = measured ? Math.min(limit - used, Math.max(0, pool.reserved)) : 0;
            return <article className="np-usage-pool" key={pool.pool_id} aria-label={label}>
              <h3>{label}</h3>
              <strong className={`np-usage-pool-value${pool.remaining === null ? " is-unlimited" : ""}`}>{pool.remaining === null ? "未设次数上限" : <><small>剩余 </small>{pool.remaining.toLocaleString("zh-CN")}<small> 次</small></>}</strong>
              {measured ? <div className="np-usage-pool-progress" role="meter" aria-label={`${label}周期占用`} aria-valuemin={0} aria-valuemax={limit} aria-valuenow={used + reserved}
                aria-valuetext={`成功已用 ${pool.success_used} 次，预留中 ${pool.reserved} 次，其中待确认 ${pool.unknown} 次，周期上限 ${limit} 次`}>
                <span className="np-usage-pool-progress-used" style={{ width: `${used / limit * 100}%` }} />
                <span className="np-usage-pool-progress-reserved" style={{ width: `${reserved / limit * 100}%` }} />
              </div> : <p hidden={limit !== 0} className="np-usage-pool-limit">{limit === 0 ? "周期上限为 0 次" : "成功与预留分别记录"}</p>}
              <dl><div><dt>成功已用</dt><dd>{pool.success_used}</dd></div><div><dt>预留中</dt><dd>{pool.reserved}</dd></div><div><dt>其中待确认</dt><dd>{pool.unknown}</dd></div><div><dt>周期上限</dt><dd>{limit ?? "未设置"}</dd></div></dl>
              {pool.admission_blocked_by_period && <p className="np-usage-note">此分类存在周期冲突，暂不能据此余额提交。</p>}
            </article>;
          })}</div>
          <p hidden className="np-usage-note">余额不保证可立即提交，仍需满足规格、权限、旧周期预留和运行保护要求。</p>
          <details hidden className="np-usage-disclosure"><summary>规格与计次规则</summary>
            <p className="np-usage-note">并行操作上限 {policy.active_operations_per_owner}；请求速率上限 {policy.request_rate_per_minute}/分钟。</p>
            <ul className="np-usage-rule-list">{Object.entries(policy.rules).map(([key, value]) => <li key={key}>{ruleLabels[key] ?? key}：{value ? "符合完整判据时计次" : "不计成功次数"}</li>)}</ul>
            <p className="np-usage-note">规格由服务端验证。成功指服务交付符合规则，不等于科学结论正确；失败、取消或未知结果不能简单当成成功。</p>
            {Object.entries(policy.modules).map(([id, item]) => <details key={id}><summary>{modules[id] ?? id}</summary>{Object.entries(item.variants).map(([variant, contract]) => <div key={variant} className="np-usage-spec"><strong>{variant === "default" ? "默认规格" : variant} · {contract.mode === "count" ? policy.pool_labels[contract.pool_id ?? ""] ?? "计次" : contract.mode === "external" ? "外部服务，不归属内部次数" : "仅请求保护，不计成功次数"}</strong><dl>{Object.entries(contract.fields).map(([name, field]) => <div key={name}><dt>{fields[name] ?? name}</dt><dd>{specification(field)}</dd></div>)}</dl></div>)}</details>)}
          </details>
          {Boolean(data.held_periods?.length) && <details className="np-usage-disclosure"><summary>查看旧周期预留</summary><ul>{data.held_periods?.map(item => <li key={`${item.pool_id}:${item.period_start}:${item.period_end}`}>{policy.pool_labels[item.pool_id] ?? item.pool_id}：{beijingTimestamp(item.period_start)} 至 {beijingTimestamp(item.period_end)}（北京时间），预留 {item.reserved}，其中待确认 {item.unknown}。</li>)}</ul></details>}
          <details className="np-usage-disclosure"><summary>请求诊断</summary>
            <div className="np-usage-diagnostics" aria-label="请求诊断"><span>HTTP任务请求：{data.request_observations?.request_count ?? "未记录"}</span><span>操作数：{data.operation_count ?? "未记录"}</span><span>执行尝试：{data.execution_attempts ?? "未记录"}</span></div>
            <p hidden className="np-usage-note">请求、操作和执行尝试是不同口径，不作为成功次数相加。</p>
            {data.request_observation_degraded && <p role="alert" className="np-usage-note">部分请求观测缺失，诊断总数可能不完整。</p>}
          </details>
        </>}
      </section>
    </div>
    <div id="usage-panel-operations" role="tabpanel" aria-labelledby="usage-tab-operations" hidden={activeTab !== "operations"} className="np-usage-tabpanel">
      <div className="np-usage-section-header"><h2 id="usage-operations-heading">操作记录</h2><div className="np-usage-record-sources" role="group" aria-label="记录来源">
        <button type="button" aria-pressed={source === "activity"} aria-controls="usage-source-activity" onClick={() => { setSelected(null); setSource("activity"); }}>任务记录</button>
        <button type="button" aria-pressed={source === "receipts"} aria-controls="usage-source-receipts" onClick={() => { setSelected(null); setSource("receipts"); }}>计次回执</button>
      </div></div>
      <div id="usage-source-activity" className="np-usage-tabpanel" hidden={source !== "activity"}>
        <UsageActivityPanel {...activity} active={activeTab === "operations" && source === "activity"} offset={activityOffset} moduleGroup={activityModuleGroup}
          onOffsetChange={setActivityOffset} onModuleGroupChange={value => { setActivityModuleGroup(value); setActivityOffset(0); }} />
      </div>
      <div id="usage-source-receipts" className="np-usage-tabpanel" hidden={source !== "receipts"}>
      <section className="np-usage-section" aria-labelledby="usage-receipts-heading" aria-busy={operations.loading}>
        <div className="np-usage-section-header"><h3 id="usage-receipts-heading">计次回执</h3><label className="np-usage-operation-filter" htmlFor="usage-operation-module">功能模块
          <WorkbenchSelect id="usage-operation-module" ariaLabel="功能模块" value={moduleGroup} constrainToScrollParent options={[{ value: "", label: "全部模块" }, ...operationGroups.map(group => ({ value: group.id, label: group.label }))]}
            onChange={value => { setModuleGroup(value); setOffset(0); setSelected(null); }} />
        </label></div>
        {data && !data.enabled && <p className="np-usage-applied-filter" role="status">计次操作记录当前未启用。<span hidden>新操作不会生成计次回执；下方仅展示历史回执。已接入任务可切换到“任务记录”查看，资源数据可在“资源观测”中查看。</span></p>}
        <p hidden className="np-usage-note">这里展示已生成的计次操作回执（含处理中），覆盖全部时间，仅按功能筛选。操作完成后点击“刷新全部”更新状态；未接入计次的功能请到原功能历史查看。</p>
        <p hidden className="np-usage-note">{USAGE_UNASSIGNED_NOTE}</p>
        <p hidden className="np-usage-note">断线或结果待确认时，先读取原操作；读取已有结果不提交、重试或重算任务。</p>
        <UsageReadFeedback loading={operations.loading} error={operations.error} hasData={Boolean(operations.data)} label="操作记录" retry={operations.retry} />
        {operations.data && (!operations.data.items.length ? <p className="np-usage-note">暂无操作回执。</p> : <>
          <div className="np-usage-table-scroll" role="region" aria-label="操作回执表，可横向滚动" tabIndex={0}><table><thead><tr><th>创建时间（北京时间）</th><th>功能模块 / 任务类型</th><th>状态</th><th>读取</th></tr></thead><tbody>{operations.data.items.map(item => <tr key={item.operation_id}>
            <td><time dateTime={item.created_at}>{beijingTimestamp(item.created_at)}</time></td>
            <td>{usageModuleGroupLabel(item.module, operationGroups)}<small className="np-usage-task-type">{usageTaskTypeLabel(item.module, modules)}</small><small className="np-usage-operation-id" title={item.operation_id}>{shortUsageTaskId(item.operation_id, item.module)}</small></td>
            <td><span className={`np-usage-state np-usage-operation-state-${item.state}`}>{operationStateLabels[item.state] ?? item.state}</span></td>
            <td><div className="np-usage-operation-actions">{[false, true].map(readResult => <button key={String(readResult)} type="button" onClick={event => { trigger.current = event.currentTarget; setSelected({ id: item.operation_id, readResult, requestKey: ++requestSequence.current }); }}>{readResult ? "读取已有结果" : "查看回执"}</button>)}</div></td>
          </tr>)}</tbody></table></div>
        </>)}
        {(operations.data || offset > 0) && <nav className="np-usage-pagination" aria-label="操作回执分页"><span>第 {Math.floor(offset / 25) + 1} 页</span><div><button type="button" disabled={offset === 0 || operations.loading} onClick={() => { setOffset(n => Math.max(0, n - 25)); setSelected(null); }}>上一页操作</button><button type="button" disabled={!operations.data?.has_more || operations.loading || offset >= 100000} onClick={() => { setOffset(n => n + 25); setSelected(null); }}>下一页操作</button></div></nav>}
        {offset >= 100000 && <p className="np-usage-note">已达分页查询上限，请调整功能筛选继续查找。</p>}
      </section>
      <UsageOperationDrawer operationId={receiptsActive ? selected?.id ?? null : null} readResultOnOpen={selected?.readResult} requestKey={selected?.requestKey ?? 0} moduleLabels={modules} moduleGroups={operationGroups} onClose={() => setSelected(null)} restoreFocusTarget={trigger.current} />
      </div>
    </div>
  </>;
}
