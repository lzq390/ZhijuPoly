import { useEffect, useRef, useState } from "react";
import { useAuth } from "../../auth/AuthProvider";
import { fetchEntitlements, fetchUsageOperation, fetchUsageOperationResult, fetchUsageOperations } from "../../services/entitlements";
import type { Entitlements, OperationState, SpecificationField, UsageOperation, UsageOperationPage, UsageOperationResult } from "../../types/entitlements";
import { utcTimestamp } from "./format";

const states: Record<OperationState,string> = { reserved:"已预留", dispatching:"派发中", running:"处理中", unknown:"结果待确认", succeeded:"已计成功次数", released:"已释放预留" };
const modules: Record<string,string> = { assistant:"通用助手",tg_assistant:"Tg 助手",recording_summary:"浏览总结",knowledge:"知识检索",online_knowledge:"在线抽取",conditional_generation:"Tg 条件生成",polytao:"PolyTAO 生成",predict:"性能预测",ocsr:"分子图像识别",retrosynthesis:"单体逆合成",reverse_design:"逆向设计",query:"相似性查询",structure:"结构处理",database_browser:"材料数据库",dft_history:"DFT 历史",monomer_md:"分子动力学",monomer_dft:"DFT",polymerization:"单体聚合",polymerization_batch:"批量聚合",batch_preflight:"导入校验",md_demo:"MD 演示",openscience:"OpenScience" };
const fields: Record<string,string> = { property_count:"性质数量",image_bytes:"图像字节",image_pixels:"图像像素",num_beams:"搜索束数",num_return_sequences:"返回候选数",max_new_tokens:"最大生成长度",max_results:"最多结果数",monomer_count:"单体数量",operation:"操作类型",atom_count:"原子数",candidate_count:"目标候选数",top_k:"top-k",top_p:"top-p",temperature:"温度",delta_tg:"目标Tg变化",max_length:"最大长度",message_count:"消息数",input_characters:"输入字符数",image_count:"图像数",max_output_tokens:"最大输出token",provider_attempt_limit:"供应商尝试上限",evidence_characters:"证据字符数",run_mode:"运行模式",protocol:"协议",requested_steps:"请求步数",requested_atom_count:"请求原子数",calculation_type:"计算类型",model:"模型",total_atoms:"总原子数",requested_properties:"请求性质",max_steps:"优化步数",conformer_max_iterations:"构象迭代上限",raw_pairs:"原始组合数",valid_pairs:"有效组合数",rows_a:"A表行数",rows_b:"B表行数",max_papers:"文献上限",mode:"模式",candidate_size:"候选库大小",match_mode:"匹配模式",similarity_threshold:"相似性阈值" };
const ruleLabels: Record<string,string> = { generation_partial_counts:"生成部分有效候选",chemistry_no_match_counts:"化学服务明确无匹配",batch_partial_counts:"批量部分完成",online_partial_counts:"在线抽取部分完成",dft_non_converged_counts:"DFT优化未收敛但完整交付",md_demo_counts:"MD演示" };
function specification(field: SpecificationField) {
  if (field.values) return field.values.join(" / ") + (field.type === "enum_list" ? `（选 ${field.min_items}–${field.max_items} 项）` : "");
  return `${field.exclusive_min ? "> " : ""}${field.min}–${field.max}`;
}
function ReadError({ message }: { message: string }) { return <p role="alert" className="np-usage-error">{message}</p>; }
const failure = (error: unknown) => error instanceof Error ? error.message : "读取失败，请重试读取。";
type ReadState<T> = { data?: T; error?: string; loading: boolean };

export function PersonalEntitlementsPanel() {
  const auth = useAuth();
  const [revision,setRevision] = useState(0), [offset,setOffset] = useState(0), [module,setModule] = useState("");
  const [balance,setBalance] = useState<ReadState<Entitlements>>({loading:true});
  const [operations,setOperations] = useState<ReadState<UsageOperationPage>>({loading:true});
  const [selected,setSelected] = useState<UsageOperation | null>(null);
  const [result,setResult] = useState<UsageOperationResult | null>(null);
  const [detailError,setDetailError] = useState("");
  const [reading,setReading] = useState(false);
  const detail = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController(); setBalance({loading:true});
    fetchEntitlements(controller.signal).then(data => { if (!controller.signal.aborted) setBalance({data,loading:false}); })
      .catch(error => { if (!controller.signal.aborted) setBalance({error:failure(error),loading:false}); });
    return () => controller.abort();
  },[revision]);
  useEffect(() => {
    const controller = new AbortController(); setOperations({loading:true});
    // Old durable results remain readable even when new allowance admission is off.
    fetchUsageOperations(offset,module || undefined,controller.signal).then(data => { if (!controller.signal.aborted) setOperations({data,loading:false}); })
      .catch(error => { if (!controller.signal.aborted) setOperations({error:failure(error),loading:false}); });
    return () => controller.abort();
  },[revision,offset,module]);
  useEffect(() => () => detail.current?.abort(),[]);
  function clearDetail() { detail.current?.abort(); setSelected(null); setResult(null); setDetailError(""); setReading(false); }
  async function readOperation(id: string, includeResult: boolean) {
    detail.current?.abort(); const controller = new AbortController(); detail.current = controller;
    setReading(true); setDetailError(""); setResult(null);
    try {
      const operation = await fetchUsageOperation(id,controller.signal);
      if (controller.signal.aborted) return;
      setSelected(operation);
      if (includeResult) {
        const recovered = await fetchUsageOperationResult(id,controller.signal);
        if (!controller.signal.aborted) { setSelected(recovered.operation); setResult(recovered); }
      }
    } catch (error) { if (!controller.signal.aborted) setDetailError(failure(error)); }
    finally { if (!controller.signal.aborted) setReading(false); }
  }
  const data = balance.data, policy = data?.policy;
  return <>
    <section className="np-usage-panel" aria-labelledby="usage-pools-heading">
      <div className="np-usage-section-header"><h2 id="usage-pools-heading">分类成功次数</h2><button type="button" onClick={() => setRevision(n=>n+1)}>刷新次数与回执</button></div>
      <p className="np-usage-note">{auth?.session.account_type === "free" ? "免费账号：会员服务尚未开放，本地画板和公开示例仍可使用。" : "会员账号的成功次数与规格以服务端策略为准。"}次数余额不会授予额外功能权限。</p>
      {balance.loading ? <p role="status">正在读取分类次数…</p> : balance.error ? <ReadError message={balance.error}/> : data && !data.enabled ? <p role="status">分类次数尚未启用。仍可读取已有操作回执；资源观测保留为诊断。</p> : data && policy && <>
        {policy.example && <p className="np-usage-coverage-warning" role="note">当前为示例策略，不代表正式套餐承诺。</p>}
        <p className="np-usage-note">当前{({day:"日",week:"周",month:"月"})[policy.period]}周期（UTC）：{utcTimestamp(data.period_start)} 至 {utcTimestamp(data.period_end)}，结束时刻不包含在内。按操作获准预留时归属周期。</p>
        <p className="np-usage-note">成功交付计次；处理中先预留。待确认次数包含在预留中，不能再次相加。读取回执或已有结果不重新执行、不新增成功次数。</p>
        {Boolean(data.held_outside_current_period) && <p className="np-usage-coverage-warning" role="note">另有 {data.held_outside_current_period} 次旧周期预留尚未结束，跨周期待确认 {data.unknown_across_periods ?? "未记录"} 次。它们仍参与运行保护，不应当作已经释放。</p>}
        {data.admission_blocked_by_period && <p role="alert" className="np-usage-coverage-warning">存在尚未结束的冲突周期，当前余额不代表可以提交；请先确认旧操作或等待原周期结束。</p>}
        <div className="np-usage-pool-grid">{data.pools.map(pool => <article className="np-usage-pool" key={pool.pool_id} aria-label={policy.pool_labels[pool.pool_id] ?? pool.pool_id}>
          <h3>{policy.pool_labels[pool.pool_id] ?? pool.pool_id}</h3><strong>{pool.remaining === null ? "未设次数上限" : `剩余 ${pool.remaining.toLocaleString("zh-CN")} 次`}</strong>
          <dl><div><dt>成功已用</dt><dd>{pool.success_used}</dd></div><div><dt>预留中</dt><dd>{pool.reserved}</dd></div><div><dt>其中待确认</dt><dd>{pool.unknown}</dd></div><div><dt>周期上限</dt><dd>{pool.limit ?? "未设置"}</dd></div></dl>
          {pool.admission_blocked_by_period && <p className="np-usage-note">此分类存在周期冲突，暂不能据此余额提交。</p>}
        </article>)}</div>
        <p className="np-usage-note">余额不保证可立即提交，仍需满足规格、权限、旧周期预留和运行保护要求。并行操作上限 {policy.active_operations_per_owner}；请求速率上限 {policy.request_rate_per_minute}/分钟。</p>
        <details><summary>规格与计次规则</summary><ul className="np-usage-rule-list">{Object.entries(policy.rules).map(([key,value])=><li key={key}>{ruleLabels[key] ?? key}：{value ? "符合完整判据时计次" : "不计成功次数"}</li>)}</ul>
          <p className="np-usage-note">规格由服务端验证。成功指服务交付符合规则，不等于科学结论正确；失败、取消或未知结果不能简单当成成功。</p>
          {Object.entries(policy.modules).map(([id,item])=><details key={id}><summary>{modules[id] ?? id}</summary>{Object.entries(item.variants).map(([variant,contract])=><div key={variant} className="np-usage-spec"><strong>{variant === "default" ? "默认规格" : variant} · {contract.mode === "count" ? policy.pool_labels[contract.pool_id ?? ""] ?? "计次" : contract.mode === "external" ? "外部服务，不归属内部次数" : "仅请求保护，不计成功次数"}</strong><dl>{Object.entries(contract.fields).map(([name,field])=><div key={name}><dt>{fields[name] ?? name}</dt><dd>{specification(field)}</dd></div>)}</dl></div>)}</details>)}
        </details>
        {Boolean(data.held_periods?.length) && <details><summary>查看旧周期预留</summary><ul>{data.held_periods?.map(item=><li key={`${item.pool_id}:${item.period_start}:${item.period_end}`}>{policy.pool_labels[item.pool_id] ?? item.pool_id}：{utcTimestamp(item.period_start)} 至 {utcTimestamp(item.period_end)}（UTC），预留 {item.reserved}，其中待确认 {item.unknown}。</li>)}</ul></details>}
        <div className="np-usage-diagnostics" aria-label="请求诊断"><span>HTTP任务请求：{data.request_observations?.request_count ?? "未记录"}</span><span>操作数：{data.operation_count ?? "未记录"}</span><span>执行尝试：{data.execution_attempts ?? "未记录"}</span></div>
        <p className="np-usage-note">请求、操作和执行尝试是不同口径，不作为成功次数相加。{data.request_observation_degraded ? "部分请求观测缺失，诊断总数可能不完整。" : ""}</p>
      </>}
    </section>
    <section className="np-usage-panel" aria-labelledby="usage-operations-heading">
      <div className="np-usage-section-header"><h2 id="usage-operations-heading">操作记录</h2><label>操作功能<select aria-label="操作功能" value={module} onChange={e=>{setModule(e.target.value);setOffset(0);clearDetail();}}><option value="">全部功能</option>{Object.entries(policy?.modules ?? modules).map(([id])=><option key={id} value={id}>{modules[id] ?? id}</option>)}</select></label></div>
      <p className="np-usage-note">断线或结果待确认时，先读取原操作。这里只读取已有回执和结果，不提交、重试或重算任务。确认结束后，在原功能中主动再次提交会创建新操作。</p>
      {operations.loading ? <p role="status">正在读取操作记录…</p> : operations.error ? <ReadError message={operations.error}/> : !operations.data?.items.length ? <p>暂无操作回执。</p> : <>
        <div className="np-usage-table-scroll" role="region" aria-label="操作回执表，可横向滚动" tabIndex={0}><table><thead><tr><th>创建时间（UTC）</th><th>功能 / 操作</th><th>状态</th><th>读取</th></tr></thead><tbody>{operations.data.items.map(item=><tr key={item.operation_id}><td>{utcTimestamp(item.created_at)}</td><td>{modules[item.module] ?? item.module}<small className="np-usage-operation-id">{item.operation_id}</small></td><td>{states[item.state] ?? item.state}</td><td><button type="button" disabled={reading} onClick={()=>void readOperation(item.operation_id,false)}>查看回执</button><button type="button" disabled={reading} onClick={()=>void readOperation(item.operation_id,true)}>读取已有结果</button></td></tr>)}</tbody></table></div>
        <nav className="np-usage-pagination" aria-label="操作回执分页"><span>第 {Math.floor(offset/25)+1} 页</span><div><button type="button" disabled={offset===0} onClick={()=>{setOffset(n=>Math.max(0,n-25));clearDetail();}}>上一页操作</button><button type="button" disabled={!operations.data.has_more} onClick={()=>{setOffset(n=>n+25);clearDetail();}}>下一页操作</button></div></nav>
      </>}
      {reading && <p role="status">正在读取已有回执或结果…</p>}{detailError && <ReadError message={detailError}/>}
      {selected && <div className="np-usage-operation-detail" aria-label="所选操作回执"><h3>{modules[selected.module] ?? selected.module} · {states[selected.state]}</h3><p>操作 {selected.operation_id}；策略 {selected.policy_version}；结果判定 {selected.outcome ?? "待确认"}。</p><p>归属周期（UTC）：{utcTimestamp(selected.period_start)} 至 {utcTimestamp(selected.period_end)}。{selected.native_task_id && `原生任务：${selected.native_task_id}`}</p><details><summary>已确认的提交规格</summary><pre>{JSON.stringify(selected.spec,null,2)}</pre></details></div>}
      {result && <div className="np-usage-operation-detail" aria-label="已恢复操作结果"><p role="status">已有结果已读取，未重新执行任务。</p>{result.result !== null && result.result !== undefined && <details open><summary>已有结果</summary><pre>{JSON.stringify(result.result,null,2)}</pre></details>}{result.result_ref !== null && result.result_ref !== undefined && <details><summary>原生结果引用</summary><pre>{JSON.stringify(result.result_ref,null,2)}</pre><p className="np-usage-note">此处展示原引用，不自动访问外部地址或重新计算。可在对应功能的任务历史中查阅产物。</p></details>}</div>}
    </section>
  </>;
}
