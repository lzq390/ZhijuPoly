import { Activity, Atom, ClipboardList, FlaskConical, LoaderCircle, LockKeyhole, Play, RefreshCw, ServerCog, TriangleAlert } from "lucide-react";
import { polymerMdServiceState } from "./serviceState";
import { PolymerMdSelect } from "./PolymerMdSelect";
import { PolymerMdConfirmation, type PolymerMdConfirmationAction } from "./PolymerMdConfirmation";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useAuth } from "../../auth/AuthProvider";
import { getSessionEpoch } from "../../auth/session";
import { requestServiceAccess } from "../../auth/guestAccess";
import type { StructureWorkspaceContext } from "../../types";
import type { PolymerMdRequest } from "../../types/polymerMd";
import { usePolymerMdSimulation } from "../../hooks/usePolymerMdSimulation";
import { POLYMER_MD_PENDING_MAX_AGE_MS, polymerMdRequestFingerprint } from "../../services/polymerMdPending";
import { MdSimulationDemoPage } from "../MdSimulationDemoPage";
import { MdStructureInput } from "../md-simulation/MdStructureInput";
import { PolymerMdResults } from "./PolymerMdResults";
import { PolymerMdAcceptanceEvidence } from "./PolymerMdAcceptanceEvidence";
import { PolymerMdConfigurationSelects, PolymerMdPropertyChoices } from "./PolymerMdConfigurationFields";
import { POLYMER_MD_BASE_PROPERTIES } from "./propertyGroups";
import { effectiveModel, PolymerMdModelFields } from "./PolymerMdModelFields";
import { defaultPolymerMdUserConfiguration, fixedPolymerMdExecutionRequest, mapPolymerMdUserConfiguration, readPolymerMdUserConfiguration, savePolymerMdUserConfiguration, type PolymerMdUserConfiguration } from "./userConfiguration";
import { jobIdFromLocation, POLYMER_MD_DEFAULTS, QUALITY_LABELS, readPolymerMdDraft, savePolymerMdDraft, stageLabel, STATUS_LABELS, terminalJob, validatePolymerMdRequest } from "./config";
const MAIN_TABS = [
  { id: "config", label: "模拟配置", description: "聚合物建模与计算参数", title: "聚合物 MD 任务配置", detail: "配置聚合物组成、模拟条件与物性计算目标。", icon: FlaskConical },
  { id: "tasks", label: "任务中心", description: "我的正式队列与历史", title: "聚合物 MD 任务中心", detail: "查看本人的正式计算任务、执行状态与历史。", icon: ClipboardList },
  { id: "results", label: "结果分析", description: "真实阶段、物性与构象", title: "聚合物 MD 结果分析", detail: "查看当前任务的科学指标、变化曲线、构象与输出文件。", icon: Activity }
] as const;
const NUMBERS: Array<{
  key: keyof PolymerMdRequest;
  label: string;
  unit: string;
  min: number;
  max: number;
  step?: number;
  help: string;
  brief: string;
}> = [{
  key: "temp",
  label: "目标温度",
  unit: "K",
  min: 100,
  max: 1000,
  help: "范围：100–1000 K；默认 300 K。用于最终平衡与物性采样。",
  brief: "100–1000 K，默认 300 K"
}, {
  key: "pressure",
  label: "目标压力",
  unit: "atm",
  min: 0,
  max: 1000,
  help: "范围：大于 0 且不超过 1000 atm；默认 1 atm。用于最终平衡与物性采样。",
  brief: "大于 0，最高 1000 atm"
}, {
  key: "n_atom",
  label: "每链目标原子数",
  unit: "个原子 / 链",
  min: 100,
  max: 5000,
  step: 1,
  help: "范围：100–5000 个原子/链，整数。用于换算聚合度，实际原子数由重复单元和端基确定。",
  brief: "100–5000 个原子／链，整数"
}, {
  key: "n_chain",
  label: "链数",
  unit: "条链",
  min: 1,
  max: 20,
  step: 1,
  help: "范围：1–20 条链，整数。还需满足基础体系总原子数上限。",
  brief: "1–20 条链，整数"

}];
export function PolymerMdFormalPage({
  structure,
  onEditStructure
}: {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
}) {
  const guest = useAuth()?.status === "guest";
  const [request, setRequest] = useState(readPolymerMdDraft);
  const [selectedId, setSelectedId] = useState(jobIdFromLocation);
  const [view, setView] = useState<"config" | "tasks" | "results">(() => jobIdFromLocation() ? "results" : "config");
  const [runMode, setRunMode] = useState<"formal" | "demo">("formal");
  const [hasOpenedDemo, setHasOpenedDemo] = useState(false);
  const [configuration, setConfiguration] = useState(readPolymerMdUserConfiguration);
  const [configurationError, setConfigurationError] = useState<string | null>(null);
  const identity = useRef(getSessionEpoch());
  const [confirmation, setConfirmation] = useState<{ action: PolymerMdConfirmationAction; jobId: string } | null>(null);
  const dismissConfirmation = useCallback(() => setConfirmation(null), []);
  useEffect(dismissConfirmation, [dismissConfirmation, runMode]);
  const selectJob = useCallback((id: string | null) => {
    setSelectedId(id);
    if (id) { setRunMode("formal"); setView("results"); }
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("job", id);else url.searchParams.delete("job");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }, []);
  const simulation = usePolymerMdSimulation(!guest && (runMode === "formal" || view === "tasks"), selectedId, selectJob);
  const {
    capabilities,
    status
  } = simulation;
  const execution = mapPolymerMdUserConfiguration(configuration, { ...request, smiles: request.smiles.trim() });
  const errors = validatePolymerMdRequest(execution.request ?? fixedPolymerMdExecutionRequest({ ...request, smiles: request.smiles.trim() }), capabilities);
  const service = polymerMdServiceState(guest, simulation.serviceError, capabilities, status);
  const { ready, reason } = service;
  useEffect(() => {
    if (identity.current === getSessionEpoch()) savePolymerMdDraft(request);
  }, [request]);
  useEffect(() => {
    const pop = () => {
      const id = jobIdFromLocation();
      setSelectedId(id);
      if (id) { setRunMode("formal"); setView("results"); }
    };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);
  const update = (change: Partial<PolymerMdRequest>) => setRequest(current => {
    const next = { ...current, ...change };
    if (next.model_configuration) {
      const model = next.model_configuration;
      next.model_configuration = { ...model, components: model.components.map((component, index) => index === 0 && change.smiles !== undefined ? { ...component, smiles: change.smiles } : component),
        ...(change.n_atom !== undefined && model.chain_length?.mode === "atoms" ? { chain_length: { mode: "atoms", value: change.n_atom } } : {}) };
    }
    return next;
  });
  const updateConfiguration = (change: Partial<PolymerMdUserConfiguration>) => setConfiguration(current => ({ ...current, ...change } as PolymerMdUserConfiguration));
  function persistConfiguration() {
    if (identity.current !== getSessionEpoch()) return;
    try { savePolymerMdUserConfiguration(configuration); setConfigurationError(null); }
    catch { setConfigurationError("浏览器暂时无法保存所选配置。请恢复会话存储后重试保存。"); }
  }
  useEffect(persistConfiguration, [configuration]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!requestServiceAccess() || !ready || !execution.request || configurationError || Object.keys(errors).length) return;
    await simulation.submit(execution.request);
  }
  function numberField(field: typeof NUMBERS[number]) {
    const value = request[field.key];
    return <label className="np-polymer-md-field" key={field.key} htmlFor={`polymer-md-${field.key}`}><span>{field.label}</span><div><input id={`polymer-md-${field.key}`} type="number" aria-label={field.label} min={field.min} max={field.max} step={field.step ?? "any"} value={typeof value === "number" && Number.isFinite(value) ? value : ""} aria-invalid={Boolean(errors[field.key])} aria-describedby={[`polymer-md-help-${field.key}`, errors[field.key] ? `polymer-md-error-${field.key}` : ""].filter(Boolean).join(" ")} onChange={e => update({
          [field.key]: e.target.value === "" ? Number.NaN : Number(e.target.value)
        })} /><small>{field.unit}</small></div><small className="np-polymer-md-field-help" id={`polymer-md-help-${field.key}`} title={field.help}>{field.brief}</small>{errors[field.key] ? <em id={`polymer-md-error-${field.key}`}>{errors[field.key]}</em> : null}</label>;
  }
  const model = effectiveModel(request);
  const estimated = model.mode === "blend" ? model.components.every(component => component.chain_length?.mode === "atoms")
    ? model.components.reduce((total, component) => total + component.chain_length!.value * component.n_chain!, 0) : null
    : model.chain_length?.mode === "atoms" ? model.chain_length.value * request.n_chain : null;
  const atomLimit = capabilities?.limits.max_total_atoms ?? 20000;
  const thermalAtomLimit = capabilities?.limits.max_rnemd_atoms ?? 60000;
  const systemLimits = `${estimated !== null && Number.isFinite(estimated) ? `估算 ${estimated.toLocaleString()} 个原子；` : "建模后核算原子数；"}基础上限 ${atomLimit.toLocaleString()}，热导体系上限 ${thermalAtomLimit.toLocaleString()} 个原子。`;
  const terminalOptions = capabilities?.supported_terminals ?? ["CH3", "H", "OH"];
  const tacticityOptions = capabilities?.supported_tacticities ?? ["atactic", "isotactic", "syndiotactic"];
  const selectedProperties = execution.executedProperties.length ? execution.executedProperties : Array.isArray(configuration.properties) ? configuration.properties : [];
  const thermalSelected = selectedProperties.includes("thermal_conductivity");
  const selectedExtensions = [thermalSelected ? "RNEMD 热导率" : null,
    selectedProperties.includes("refractive_index") ? "静态折射率" : null,
    selectedProperties.includes("thermal_diffusivity") ? "热扩散率" : null,
    selectedProperties.includes("tg") ? "玻璃化转变温度 Tg" : null,
    selectedProperties.some(key => key === "sp_total" || key === "sp_ced") ? "溶解度参数 SP / 内聚能密度 CED" : null].filter(Boolean);
  const legacyConfiguration = typeof configuration.__configuration_version === "string";
  const hasLegacyRunOverrides = (["eq_ns", "additional_ns", "max_additional", "sample_interval_ps", "tc_ns", "tc_rep", "seed"] as const)
    .some(key => request[key] !== POLYMER_MD_DEFAULTS[key]);
  function selectField(key: "terminal_head" | "terminal_tail" | "tacticity" | "tc_axis", label: string, options: readonly string[]) {
    const help = { terminal_head: "可选 CH₃（甲基）、H（氢）、OH（羟基），首末端可分别设置。",
      terminal_tail: "可选 CH₃（甲基）、H（氢）、OH（羟基），用于聚合物链封端。",
      tacticity: "可选无规、全同、间同，控制适用重复单元的立体序列；不代表结晶度，无相关手性中心时可能没有区别。",
      tc_axis: "可选 x、y、z；计算所选材料方向上的总热导率。" }[key];
    const brief = { terminal_head: "CH₃、H 或 OH，独立设置首端", terminal_tail: "CH₃、H 或 OH，独立设置末端",
      tacticity: "无规、全同或间同", tc_axis: "选择 x、y 或 z 方向" }[key];
    return <PolymerMdSelect id={`polymer-md-${key}`} label={label} value={request[key]}
      invalid={Boolean(errors[key])} describedBy={[`polymer-md-help-${key}`, errors[key] ? `polymer-md-error-${key}` : ""].filter(Boolean).join(" ")}
      options={options.map(value => ({ value, label: ({ atactic: "无规", isotactic: "全同", syndiotactic: "间同" } as Record<string, string>)[value] ?? value }))}
      onChange={value => update({ [key]: value })}>
      <small className="np-polymer-md-field-help" id={`polymer-md-help-${key}`} title={help}>{brief}</small>{errors[key] ? <em id={`polymer-md-error-${key}`}>{errors[key]}</em> : null}
    </PolymerMdSelect>;
  }
  const pending = simulation.pendingSubmission;
  const changedPendingInput = pending && (!execution.request || polymerMdRequestFingerprint(execution.request) !== polymerMdRequestFingerprint(pending.request));
  const pendingExpired = pending && (Date.now() - pending.createdAt > POLYMER_MD_PENDING_MAX_AGE_MS || pending.createdAt > Date.now());
  const pendingNotice = pending || simulation.pendingIssue ? <section className="np-polymer-md-notice" aria-label="待确认提交"><h2>上次提交结果尚未确认</h2><p>请求可能已被受理。恢复原提交会使用已保存的参数和同一标识确认任务，不会自动替换为当前输入。</p>{pendingExpired ? <p>此记录已超过 24 小时或时间信息已变化。请先检查“任务中心”，再选择恢复原提交或放弃恢复。</p> : null}{changedPendingInput ? <p>当前输入已更改；恢复时仍使用上次保存的参数。放弃恢复后才可提交当前输入。</p> : null}{pending?.conflict ? <p role="alert">原提交标识发生冲突。请检查“任务中心”后放弃恢复；不会自动改用新标识。</p> : null}{simulation.pendingIssue ? <p role="alert">{simulation.pendingIssue}</p> : null}<p>放弃恢复只清除本地记录，不会取消可能已在后台运行的任务；再次提交可能产生新的计算。</p><div className="np-polymer-md-actions">{pending ? <button type="button" disabled={!ready || Boolean(simulation.busy) || pending.conflict} onClick={() => void simulation.recoverSubmission()}>恢复原提交</button> : null}<button type="button" disabled={Boolean(simulation.busy)} onClick={() => simulation.discardSubmission()}>放弃恢复（不取消后台任务）</button></div></section> : null;
  const confirmedJob = confirmation ? [simulation.job, ...(simulation.history?.items ?? [])].find(job => job?.job_id === confirmation.jobId) : null;
  const confirmationAllowed = confirmedJob && confirmation && (confirmation.action === "cancel"
    ? !terminalJob(confirmedJob.status) && confirmedJob.status !== "cancel_requested"
    : terminalJob(confirmedJob.status));
  function confirmMutation() {
    if (!confirmation || !confirmationAllowed || simulation.busy || identity.current !== getSessionEpoch()) return;
    const { action, jobId } = confirmation;
    setConfirmation(null);
    void simulation.mutate(action, jobId);
  }
  const activeTab = MAIN_TABS.find(tab => tab.id === view) ?? MAIN_TABS[0];
  const SurfaceIcon = activeTab.icon;
  const serviceTone = ready ? "ready" : guest ? "guest" : simulation.serviceError ? "error" : !capabilities || !status ? "loading" : "unavailable";
  const StatusIcon = guest ? LockKeyhole : simulation.serviceError ? TriangleAlert : serviceTone === "loading" ? LoaderCircle : TriangleAlert;
  const changeDemoView = useCallback((next: "input" | "results") => setView(next === "results" ? "results" : "config"), []);

  return <div className="np-pmd-page np-module-page-body">
    <div className="np-pmd-module-toolbar" aria-label="聚合物 MD 模拟工具栏">
      <div className="np-pmd-service-status">
        <span className={`is-${serviceTone}`} role="status" title={reason}>
          {ready ? <i className="np-pmd-ready-dot" aria-hidden="true" /> : <StatusIcon className={serviceTone === "loading" ? "np-sw-spin" : undefined} aria-hidden="true" />}
          <span className="np-pmd-service-copy"><strong>{service.label}</strong>{!ready ? <small>{reason}</small> : null}</span>
        </span>
        <button type="button" onClick={() => { if (requestServiceAccess()) void simulation.refresh(); }}><RefreshCw aria-hidden="true" />刷新</button>
      </div>
    </div>
    <div className="np-pmd-scroll-region">
      <div className="np-pmd-content-column">
        <main className="np-pmd-workbench-surface np-sw-accented-surface" aria-label="聚合物 MD 主工作区">
          <header className="np-pmd-view-header">
            <div className="np-pmd-view-heading"><span className="np-pmd-surface-mark"><SurfaceIcon aria-hidden="true" /></span><div><h2>{view === "results" && runMode === "demo" ? "聚合物 MD 示例结果" : activeTab.title}</h2><p>{view === "results" && runMode === "demo" ? "查看真实示例计算的物性、曲线与轨迹分析。" : activeTab.detail}</p></div></div>
            {view !== "config" ? <span className="np-pmd-view-badge"><ServerCog aria-hidden="true" />{view === "tasks" ? "我的任务" : runMode === "demo" ? "真实示例结果" : "真实计算结果"}</span> : null}
          </header>
          <nav className="np-pmd-main-tabs" role="tablist" aria-label="聚合物 MD 主工作区" onKeyDown={event => {
            const tabs = ["config", "tasks", "results"] as const;
            const delta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
            if (!delta && !["Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[2] : tabs[(tabs.indexOf(view) + delta + tabs.length) % tabs.length];
            setView(next); document.getElementById(`polymer-md-tab-${next}`)?.focus();
          }}>
            {MAIN_TABS.map(({ id, label, description, icon: Icon }) => <button key={id} id={`polymer-md-tab-${id}`} type="button" role="tab" aria-label={label} aria-selected={view === id} aria-controls="polymer-md-workspace-panel" tabIndex={view === id ? 0 : -1} className={view === id ? "is-active" : undefined} onClick={() => setView(id)}><Icon aria-hidden="true" /><span><strong>{label}</strong><small>{description}</small></span></button>)}
          </nav>
          <section id="polymer-md-workspace-panel" className="np-pmd-main-panel" role="tabpanel" aria-labelledby={`polymer-md-tab-${view}`}>
            {simulation.error ? <p role="alert" className="np-polymer-md-notice">{simulation.error}</p> : null}
            {pendingNotice}
            {view === "config" ? <div className="np-pmd-config-workspace">
              <div className="np-pmd-mode-switch" role="group" aria-label="聚合物 MD 运行模式" data-run-mode={runMode}>
                <span className="np-pmd-mode-switch__indicator" aria-hidden="true" />
                <button type="button" aria-label="正式计算" aria-pressed={runMode === "formal"} className={runMode === "formal" ? "is-active" : undefined} onClick={() => setRunMode("formal")}><FlaskConical aria-hidden="true" /><span><strong>正式计算</strong><small>聚合物建模 · 异步物性计算</small></span></button>
                <button type="button" aria-label="示例演示" aria-pressed={runMode === "demo"} className={runMode === "demo" ? "is-active" : undefined} onClick={() => { setHasOpenedDemo(true); setRunMode("demo"); }}><Atom aria-hidden="true" /><span><strong>示例演示</strong><small>真实示例结果 · 完整分析流程</small></span></button>
              </div>
              {runMode === "formal" ? <form className="np-pmd-config-form" onSubmit={submit} noValidate>
      <section className="np-polymer-md-card" aria-labelledby="polymer-md-structure-title"><span className="np-polymer-md-eyebrow">结构输入</span><h2 id="polymer-md-structure-title">聚合物组成</h2><PolymerMdModelFields request={request} onChange={update} error={errors.model_configuration} /><p>重复单元 A：输入包含两个 * 连接点的中性闭壳层重复单元。</p><MdStructureInput value={request.smiles} error={errors.smiles ?? null} onChange={smiles => update({ smiles })} onTouched={() => {}} onEditStructure={onEditStructure} /></section>
      <section className="np-polymer-md-card np-pmd-system-section" aria-labelledby="polymer-md-basic-title"><span className="np-polymer-md-eyebrow">模型与条件</span><h2 id="polymer-md-basic-title">聚合物体系</h2>
        <p className="np-polymer-md-muted np-pmd-system-summary" title="材料在具体温压下是否适用，取决于聚合物与力场；前置装填、压缩阶段按平台方案执行。">设置温压、体系规模、端基及力场。</p>
        <div className="np-polymer-md-fields np-pmd-system-fields">{NUMBERS.filter(field => field.key !== "n_atom" || model.chain_length?.mode === "atoms").filter(field => field.key !== "n_chain" || model.mode !== "blend").map(numberField)}{selectField("terminal_head", "首端端基", terminalOptions)}{selectField("terminal_tail", "末端端基", terminalOptions)}{selectField("tacticity", "立构规整性", tacticityOptions)}<PolymerMdConfigurationSelects section="model" configuration={configuration} onChange={updateConfiguration} errors={errors} inline /></div>
        <div className="np-polymer-md-system-limits" aria-label="体系规模说明"><p title={`${systemLimits}提交前预估规模，建模后核对实际原子数；热导上限按 RNEMD 复制后的体系计算。`}>{systemLimits}</p></div>
        {model.mode === "blend" && errors.n_chain ? <p role="alert">{errors.n_chain}请减少组分链数或每链规模后重试。</p> : null}
      </section>
      <section className="np-polymer-md-card np-pmd-quantum-section" aria-labelledby="polymer-md-quantum-title"><span className="np-polymer-md-eyebrow">MD 建模准备</span><h2 id="polymer-md-quantum-title">量子化学方法</h2><p className="np-polymer-md-muted np-pmd-quantum-summary" title="几何优化和原子电荷用于建模，各重复单元分别计算；静态折射率需要的极化率也在准备阶段计算。">设置几何优化、电荷与静态极化率方法。</p><PolymerMdConfigurationSelects section="quantum" configuration={configuration} onChange={updateConfiguration} /></section>
      <PolymerMdPropertyChoices configuration={configuration} onChange={updateConfiguration} />
      {thermalSelected ? <section className="np-polymer-md-card" aria-label="热导方向"><h2>传热方向</h2><div className="np-polymer-md-fields">{selectField("tc_axis", "传热方向", ["x", "y", "z"])}</div><p className="np-polymer-md-muted">所选热导性质使用此材料方向。运行时长与体系复制由平台运行方案管理。</p>{errors.tc_rep ? <p role="alert">{errors.tc_rep}请减少链数或每链规模后重试。</p> : null}</section> : null}
      <section className="np-polymer-md-platform-note" aria-label="平台运行方案"><h2>平台运行方案</h2><p>运行方案由平台统一管理，自动安排平衡与采样；具体运行条件随任务结果记录。</p>{hasLegacyRunOverrides ? <p>旧草稿的运行安排已由平台默认方案替代；已提交任务保留原方案。</p> : null}</section>
      <div className="np-polymer-md-execution-state" aria-label="所选配置执行状态">
        {configurationError ? <><p role="alert">{configurationError}</p><button type="button" onClick={persistConfiguration}>重试保存配置</button></> : !execution.request ? <p role="status">请检查所选配置；旧版或无法识别的选择已保留，请迁移或恢复默认参数。</p> : <p>默认统一分析 {POLYMER_MD_BASE_PROPERTIES.length} 项基础物性。{selectedExtensions.length ? `追加：${selectedExtensions.join("、")}。` : "未选择追加计算。"}提交时使用所选力场、量化方法和基组。</p>}
        {!execution.request && execution.unmapped.length ? <details open><summary>查看需要处理的配置</summary><ul>{execution.unmapped.map((reason, index) => <li key={index}>{reason}</li>)}</ul></details> : null}
        {legacyConfiguration ? <button type="button" onClick={() => setConfiguration(current => Object.fromEntries(Object.entries(current).filter(([key]) => key !== "__configuration_version")))}>已核对，迁移到当前配置版本</button> : null}
      </div>
      <footer className="np-pmd-submit-bar">
        <div className="np-pmd-submit-summary"><span>提交状态</span><strong>{guest ? "登录后提交正式计算" : !execution.request || Object.keys(errors).length ? "请检查计算配置" : ready ? "正式计算可提交" : service.label}</strong><small>已有任务保留其提交参数，修改配置不会改变既有结果。</small></div>
        <div className="np-pmd-submit-actions">
          <button type="button" onClick={() => { setRequest({ ...POLYMER_MD_DEFAULTS }); setConfiguration(defaultPolymerMdUserConfiguration()); }}>恢复默认参数</button>
          <button type="submit" className="is-primary" disabled={!guest && (!ready || Boolean(simulation.busy) || Boolean(pending) || Boolean(simulation.pendingIssue) || !execution.request || Boolean(configurationError) || Object.keys(errors).length > 0)}>{simulation.busy === "submit" ? <LoaderCircle className="np-sw-spin" aria-hidden="true" /> : <Play aria-hidden="true" />}{simulation.busy === "submit" ? "正在提交…" : "提交正式计算"}</button>
        </div>
      </footer>
              </form> : null}
              {runMode === "formal" && (status?.runtime || status?.resource_limits || status?.acceptance_summary) ? <details className="np-polymer-md-runtime"><summary>运行环境与验收状态</summary>
                <PolymerMdAcceptanceEvidence capabilities={status} />
                {status.resource_limits ? <p>当前执行资源：最多 {status.resource_limits.cpu_count} CPU、{status.resource_limits.memory_bytes / 1024 ** 3} GiB 内存；单阶段最长 {status.resource_limits.stage_timeout_seconds / 3600} 小时，单任务最长 {status.resource_limits.job_timeout_seconds / 3600} 小时。</p> : <p>当前未获取到实际资源上限。</p>}
              </details> : null}
            </div> : null}
            {hasOpenedDemo ? <div hidden={runMode !== "demo" || view === "tasks"} className="np-pmd-demo-slot"><MdSimulationDemoPage structure={structure} onEditStructure={onEditStructure} embedded active={runMode === "demo" && view !== "tasks"} workspaceView={view === "results" ? "results" : "input"} onWorkspaceViewChange={changeDemoView} /></div> : null}
            {view === "tasks" ? <section className="np-pmd-task-center">
              <header className="np-pmd-section-heading"><div><h3>我的计算任务</h3><p>查看正式计算的执行状态与历史，选择任务后继续分析。</p></div>{simulation.history ? <span>共 {simulation.history.total} 个任务</span> : null}</header>
              {simulation.historyError ? <p role="alert" className="np-polymer-md-notice">{simulation.historyError}</p> : null}
              {simulation.history?.items.length ? <ul className="np-polymer-md-jobs">{simulation.history.items.map(j => <li key={j.job_id} className="np-pmd-task-row"><div className="np-pmd-task-identity"><strong className="np-pmd-code">{j.scientific_request.smiles}</strong><small className="np-pmd-code">{j.job_id}</small><span>{stageLabel(j.stage)} · {QUALITY_LABELS[j.quality_status]}</span></div><span className={`np-pmd-status-pill is-${j.status}`}>{STATUS_LABELS[j.status]}</span><div className="np-pmd-task-actions"><button type="button" onClick={() => selectJob(j.job_id)}>查看任务</button>{!terminalJob(j.status) ? <button type="button" disabled={Boolean(simulation.busy) || j.status === "cancel_requested"} onClick={() => setConfirmation({ action: "cancel", jobId: j.job_id })}>{j.status === "cancel_requested" ? "正在取消" : "取消"}</button> : null}</div></li>)}</ul> : <div className="np-pmd-empty-state"><ClipboardList aria-hidden="true" /><h3>暂无任务记录。</h3><p>提交正式计算后，可在这里查看任务状态与历史。</p></div>}
              <div className="np-pmd-pagination"><button type="button" disabled={simulation.page <= 1} onClick={() => simulation.setPage(p => p - 1)}>上一页</button><span>第 {simulation.page} 页{simulation.history ? ` · 共 ${simulation.history.total} 个任务` : ""}</span><button type="button" disabled={!simulation.history || simulation.page * 10 >= simulation.history.total} onClick={() => simulation.setPage(p => p + 1)}>下一页</button></div>
            </section> : null}
            {view === "results" && runMode === "formal" ? simulation.loading && !simulation.job ? <div className="np-pmd-empty-state" role="status"><LoaderCircle className="np-sw-spin" aria-hidden="true" /><h3>正在读取所选任务…</h3><p>正在读取任务的最新状态和已有结果。</p></div> : simulation.job ? <PolymerMdResults key={simulation.job.job_id} job={simulation.job} capabilities={capabilities} busy={simulation.busy} onCancel={() => setConfirmation({ action: "cancel", jobId: simulation.job!.job_id })} onClear={() => setConfirmation({ action: "clear", jobId: simulation.job!.job_id })} onDelete={() => setConfirmation({ action: "delete", jobId: simulation.job!.job_id })} /> : <div className="np-pmd-empty-state"><Activity aria-hidden="true" /><h3>{selectedId ? "当前任务不可读取" : "尚未选择计算任务"}</h3><p>{selectedId ? "当前任务不可读取，请刷新或从我的任务重新选择。" : "提交计算或在“任务中心”中选择任务后查看结果。"}</p></div> : null}
          </section>
        </main>
      </div>
    </div>
    {confirmation && confirmationAllowed ? <PolymerMdConfirmation action={confirmation.action} jobId={confirmation.jobId} onDismiss={dismissConfirmation} onConfirm={confirmMutation} /> : null}
  </div>;
}
