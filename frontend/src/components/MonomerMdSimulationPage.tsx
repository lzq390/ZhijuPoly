import { BrowsingRecordingControls } from "./browsing-recording/BrowsingRecording";
import { ModulePageHeader } from "./ModulePageHeader";
import {
  Activity,
  Atom,
  CircleOff,
  ClipboardList,
  FlaskConical,
  LoaderCircle,
  Play,
  RefreshCw,
  ServerCog,
  TriangleAlert
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { getMonomerMdSmilesValidationError, useMonomerMdSimulation } from "../hooks/useMonomerMdSimulation";
import { hasInvalidMonomerMdJobSearch } from "../lib/monomerMdRouting";
import type {
  MonomerMdFormalProtocol,
  MonomerMdRunMode,
  StructureWorkspaceContext
} from "../types";
import { MonomerMdFormalConfig } from "./monomer-md-simulation/MonomerMdFormalConfig";
import { MonomerMdResultsPanel } from "./monomer-md-simulation/MonomerMdResultsPanel";
import { MonomerMdStructureInput } from "./monomer-md-simulation/MonomerMdStructureInput";
import { MonomerMdTaskCenter } from "./monomer-md-simulation/MonomerMdTaskCenter";
import {
  cloneConfig,
  configFingerprint,
  FORMAL_PROTOCOLS,
  isRecord,
  restoreManagedPaths,
  validateFormalConfig
} from "./monomer-md-simulation/config";
import {
  loadMonomerMdSession,
  saveMonomerMdSession
} from "./monomer-md-simulation/session";
import {
  formatNumber,
  translateMonomerMdMessage
} from "./monomer-md-simulation/presentation";
import "../styles/structure-workbench.css";
import "../styles/monomer-md-simulation.css";

type MainTab = "config" | "tasks" | "results";

type MonomerMdSimulationPageProps = {
  structure: StructureWorkspaceContext;
  initialJobId: string | null;
  onJobIdChange: (jobId: string | null) => void;
  onEditStructure: () => void;
};

const MAIN_TABS: Array<{
  id: MainTab;
  label: string;
  description: string;
  surfaceTitle: string;
  surfaceDescription: string;
  badge: string | null;
  icon: typeof FlaskConical;
}> = [
  {
    id: "config",
    label: "模拟配置",
    description: "快速演示与完整 MD",
    surfaceTitle: "单体 MD 任务配置",
    surfaceDescription: "提交快速Density模拟演示，或正式MD模拟任务",
    badge: null,
    icon: FlaskConical
  },
  {
    id: "tasks",
    label: "任务中心",
    description: "全局正式队列与历史",
    surfaceTitle: "全局 MD 任务中心",
    surfaceDescription: "查看正式活跃队列、排队位置与全局正式任务历史。",
    badge: "全局任务",
    icon: ClipboardList
  },
  {
    id: "results",
    label: "结果分析",
    description: "真实进度、曲线与构象",
    surfaceTitle: "单体 MD 结果分析",
    surfaceDescription: "查看当前任务的真实阶段、科学指标、变化曲线与构象轨迹。",
    badge: "真实计算结果",
    icon: Activity
  }
];

export function MonomerMdSimulationPage({
  structure,
  initialJobId,
  onJobIdChange,
  onEditStructure
}: MonomerMdSimulationPageProps) {
  const restoredSession = useRef(loadMonomerMdSession()).current;
  const [activeTab, setActiveTab] = useState<MainTab>(initialJobId ? "results" : "config");
  const [runMode, setRunMode] = useState<MonomerMdRunMode>(restoredSession.runMode);
  const [demoSmiles, setDemoSmiles] = useState(restoredSession.demoSmiles);
  const [demoTouched, setDemoTouched] = useState(false);
  const [selectedProtocol, setSelectedProtocol] = useState<MonomerMdFormalProtocol>(restoredSession.selectedProtocol);
  const [configs, setConfigs] = useState(restoredSession.configs);
  const [templateFingerprints, setTemplateFingerprints] = useState(restoredSession.templateFingerprints);
  const [templateChanges, setTemplateChanges] = useState<Set<MonomerMdFormalProtocol>>(new Set());

  const simulation = useMonomerMdSimulation({
    initialJobId,
    onJobIdChange,
    taskCenterActive: activeTab === "tasks"
  });

  const invalidDeepLink =
    typeof window !== "undefined" &&
    hasInvalidMonomerMdJobSearch(window.location.search);

  useEffect(() => {
    if (initialJobId) setActiveTab("results");
  }, [initialJobId]);

  useEffect(() => {
    const catalog = simulation.protocolCatalog;
    if (!catalog) return;
    const nextConfigs = { ...configs };
    const nextFingerprints = { ...templateFingerprints };
    const changed = new Set(templateChanges);
    let configsChanged = false;
    let fingerprintsChanged = false;
    let changesChanged = false;
    for (const protocol of FORMAL_PROTOCOLS) {
      const template = catalog.protocols.find((item) => item.protocol === protocol)?.default_config;
      if (!isRecord(template) || !validateFormalConfig(template, protocol).valid) continue;
      const fingerprint = configFingerprint(template);
      if (!nextConfigs[protocol]) {
        nextConfigs[protocol] = cloneConfig(template);
        nextFingerprints[protocol] = fingerprint;
        configsChanged = true;
        fingerprintsChanged = true;
      } else if (nextFingerprints[protocol] && nextFingerprints[protocol] !== fingerprint) {
        if (!changed.has(protocol)) {
          changed.add(protocol);
          changesChanged = true;
        }
      } else if (!nextFingerprints[protocol]) {
        nextFingerprints[protocol] = fingerprint;
        fingerprintsChanged = true;
      }
    }
    if (configsChanged) setConfigs(nextConfigs);
    if (fingerprintsChanged) setTemplateFingerprints(nextFingerprints);
    if (changesChanged) setTemplateChanges(changed);
    // The catalog object is the only trigger; state snapshots are intentionally handled atomically here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [simulation.protocolCatalog]);

  useEffect(() => {
    saveMonomerMdSession({
      runMode,
      demoSmiles,
      selectedProtocol,
      configs,
      templateFingerprints
    });
  }, [configs, demoSmiles, runMode, selectedProtocol, templateFingerprints]);

  const currentConfig = configs[selectedProtocol] ?? null;
  const demoValidationError = demoTouched
    ? getMonomerMdSmilesValidationError(demoSmiles)
    : null;
  const demoCanSubmit =
    !simulation.isStatusLoading &&
    simulation.statusError == null &&
    simulation.serviceStatus?.can_submit === true;
  const formalCanSubmit =
    !simulation.isStatusLoading &&
    simulation.statusError == null &&
    simulation.serviceStatus?.formal_can_submit === true;

  const servicePresentation = useMemo(() => {
    if (simulation.isStatusLoading && !simulation.serviceStatus) {
      return { tone: "loading", icon: LoaderCircle, title: "服务检查中", detail: "正在读取快速演示与完整 MD 任务容量" };
    }
    if (simulation.statusError || simulation.serviceStatus?.available === false) {
      return { tone: "error", icon: CircleOff, title: "服务不可用", detail: translateMonomerMdMessage(simulation.statusError || simulation.serviceStatus?.message) || "后端未报告可用状态" };
    }
    if (simulation.serviceStatus?.draining) {
      return { tone: "warning", icon: ServerCog, title: "部署排空", detail: "可只读查看已有任务，暂不接受新提交" };
    }
    if (!demoCanSubmit && !formalCanSubmit) {
      return { tone: "warning", icon: TriangleAlert, title: "容量已满", detail: "快速演示与完整 MD 提交当前均已关闭" };
    }
    return { tone: "ready", icon: null, title: "准备就绪", detail: null };
  }, [demoCanSubmit, formalCanSubmit, simulation.isStatusLoading, simulation.serviceStatus, simulation.statusError]);

  function changeMainTab(next: MainTab) {
    setActiveTab(next);
  }

  function handleMainTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const currentIndex = MAIN_TABS.findIndex((tab) => tab.id === activeTab);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? MAIN_TABS.length - 1
        : (currentIndex + (event.key === "ArrowRight" ? 1 : -1) + MAIN_TABS.length) % MAIN_TABS.length;
    const next = MAIN_TABS[nextIndex];
    setActiveTab(next.id);
    document.getElementById(`monomer-md-main-tab-${next.id}`)?.focus();
  }

  async function submitDemo() {
    setDemoTouched(true);
    const error = getMonomerMdSmilesValidationError(demoSmiles);
    if (error || !demoCanSubmit) return;
    const jobId = await simulation.submit({
      run_mode: "demo",
      protocol: "DensityDemo",
      smiles: demoSmiles.trim()
    });
    if (jobId) setActiveTab("results");
  }

  async function submitFormal() {
    if (!currentConfig || !formalCanSubmit) return;
    const validation = validateFormalConfig(currentConfig, selectedProtocol);
    if (!validation.valid) return;
    const jobId = await simulation.submit({
      run_mode: "formal",
      protocol: selectedProtocol,
      config_json: restoreManagedPaths(currentConfig)
    });
    if (jobId) setActiveTab("results");
  }

  function restoreProtocolTemplate(protocol: MonomerMdFormalProtocol) {
    const template = simulation.protocolCatalog?.protocols.find((item) => item.protocol === protocol)?.default_config;
    if (!isRecord(template) || !validateFormalConfig(template, protocol).valid) return;
    setConfigs((current) => ({ ...current, [protocol]: cloneConfig(template) }));
    setTemplateFingerprints((current) => ({ ...current, [protocol]: configFingerprint(template) }));
    setTemplateChanges((current) => {
      const next = new Set(current);
      next.delete(protocol);
      return next;
    });
  }

  function keepChangedTemplates() {
    const fingerprints = { ...templateFingerprints };
    for (const protocol of templateChanges) {
      const template = simulation.protocolCatalog?.protocols.find((item) => item.protocol === protocol)?.default_config;
      if (isRecord(template)) fingerprints[protocol] = configFingerprint(template);
    }
    setTemplateFingerprints(fingerprints);
    setTemplateChanges(new Set());
  }

  function restoreChangedTemplates() {
    for (const protocol of templateChanges) restoreProtocolTemplate(protocol);
    setTemplateChanges(new Set());
  }

  const StatusIcon = servicePresentation.icon;
  const activeMainTab = MAIN_TABS.find((tab) => tab.id === activeTab) ?? MAIN_TABS[0];
  const SurfaceIcon = activeMainTab.icon;
  const demoSteps = simulation.serviceStatus?.default_steps ?? 300;

  return (
    <div className="np-module-page np-structure-workbench np-monomer-md-simulation">
      <ModulePageHeader actions={<BrowsingRecordingControls module="monomerMdSimulation" />}>单体 MD 模拟</ModulePageHeader>
      <div className="np-mmd-page np-module-page-body">
        <div className="np-mmd-module-toolbar" aria-label="单体 MD 模拟工具栏">
          <div className="np-mmd-toolbar-actions">
            <div className="np-mmd-service-status">
              <span className={`is-${servicePresentation.tone}`} role="status" title={servicePresentation.detail ?? undefined}>
                {servicePresentation.tone === "ready" ? <i className="np-mmd-ready-dot" aria-hidden="true" /> : null}
                {StatusIcon ? <StatusIcon className={servicePresentation.tone === "loading" ? "np-mmd-spin" : ""} /> : null}
                <span className="np-mmd-service-status__copy">
                  <strong>{servicePresentation.title}</strong>
                  {servicePresentation.detail ? <small>{servicePresentation.detail}</small> : null}
                </span>
              </span>
              <button type="button" onClick={() => void simulation.refreshStatus()} disabled={simulation.isStatusLoading}>
                <RefreshCw className={simulation.isStatusLoading ? "np-mmd-spin" : ""} />刷新
              </button>
            </div>
          </div>
        </div>
        <div className="np-mmd-scroll-region">
          <div className="np-mmd-content-column">
            {invalidDeepLink ? (
              <div className="np-mmd-deep-link-error" role="alert">
                <TriangleAlert /><div><strong>任务深链格式无效</strong><span>job 必须是 32 位十六进制 ID；本页没有向后端发送该查询。</span></div>
              </div>
            ) : null}

            <main className="np-mmd-workbench-surface np-sw-accented-surface" aria-label="单体 MD 主工作区">
              <header className="np-mmd-view-header">
                <div className="np-mmd-view-heading">
                  <span className="np-mmd-surface-mark"><SurfaceIcon /></span>
                  <div>
                    <h2>{activeMainTab.surfaceTitle}</h2>
                    <p>{activeMainTab.surfaceDescription}</p>
                  </div>
                </div>
                {activeMainTab.badge ? <span className="np-mmd-view-badge"><ServerCog />{activeMainTab.badge}</span> : null}
              </header>
              <div className="np-mmd-main-tabs" role="tablist" aria-label="单体 MD 主工作区" onKeyDown={handleMainTabKeyDown}>
                {MAIN_TABS.map((tab) => {
                  const Icon = tab.icon;
                  return (
                    <button
                      key={tab.id}
                      id={`monomer-md-main-tab-${tab.id}`}
                      type="button"
                      role="tab"
                      aria-selected={activeTab === tab.id}
                      aria-controls={`monomer-md-main-panel-${tab.id}`}
                      tabIndex={activeTab === tab.id ? 0 : -1}
                      className={activeTab === tab.id ? "is-active" : ""}
                      onClick={() => changeMainTab(tab.id)}
                    >
                      <Icon /><span><strong>{tab.label}</strong><small>{tab.description}</small></span>
                      {tab.id === "tasks" && simulation.activeJobs.length ? <b>{simulation.activeJobs.length}</b> : null}
                      {tab.id === "results" && simulation.job ? <i className={`is-${simulation.job.status}`} /> : null}
                    </button>
                  );
                })}
              </div>

              <section
                id={`monomer-md-main-panel-${activeTab}`}
                role="tabpanel"
                aria-labelledby={`monomer-md-main-tab-${activeTab}`}
                className="np-mmd-main-panel"
              >
                {activeTab === "config" ? (
                  <div className="np-mmd-config-workspace">
                    <div className="np-mmd-mode-switch" role="group" aria-label="模拟运行模式">
                      <button type="button" className={runMode === "demo" ? "is-active" : ""} onClick={() => setRunMode("demo")}><Atom /><span><strong>快速演示</strong><small>DensityDemo · 真实 Worker MD</small></span></button>
                      <button type="button" className={runMode === "formal" ? "is-active" : ""} onClick={() => setRunMode("formal")}><FlaskConical /><span><strong>完整MD模拟</strong><small>ByteFF2 · 五种科研物性任务</small></span></button>
                    </div>

                    {runMode === "demo" ? (
                      <div className="np-mmd-demo-config">
                        <div className="np-mmd-section-heading">
                          <div><span className="np-mmd-eyebrow">DENSITY DEMO</span><h3>真实密度演示</h3><p>提交后由 Worker 真实执行 ByteFF2 NPT 分子动力学；由于演示步数较短，不能作为平衡密度估计。</p></div>
                          <div className="np-mmd-readonly-steps"><span>真实执行步数</span><strong>{formatNumber(demoSteps, 0)}</strong><small>Worker 服务只读配置</small></div>
                        </div>
                        <MonomerMdStructureInput
                          value={demoSmiles}
                          error={demoValidationError}
                          onChange={(value) => { setDemoSmiles(value); if (demoTouched) setDemoTouched(true); }}
                          getSharedSmiles={structure.getCurrentSmiles}
                          onEditStructure={onEditStructure}
                        />
                        <div className="np-mmd-demo-warning"><TriangleAlert /><div><strong>真实计算，但尚未平衡</strong><span>Worker 会实际运行 MD；演示步数不足以使体系达到平衡，不能作为物理密度结论。</span></div></div>
                        <div className="np-mmd-submit-bar">
                          <div><span>提交状态</span><strong className="np-mmd-submit-status">{demoCanSubmit ? "真实模拟可提交" : servicePresentation.title}</strong><small>关闭提交时仍可浏览已有真实任务和结果</small></div>
                          <div className="np-mmd-submit-bar__action"><span>{demoValidationError || (demoCanSubmit ? "将创建真实异步 Worker 任务，后端会继续验证化学结构" : "当前无法创建演示任务")}</span><button type="button" disabled={simulation.isSubmitting || !demoCanSubmit || Boolean(getMonomerMdSmilesValidationError(demoSmiles))} onClick={() => void submitDemo()}>{simulation.isSubmitting ? <LoaderCircle className="np-mmd-spin" /> : <Play />}{simulation.isSubmitting ? "正在创建真实任务" : "开始快速模拟"}</button></div>
                        </div>
                      </div>
                    ) : (
                      <MonomerMdFormalConfig
                        protocol={selectedProtocol}
                        config={currentConfig}
                        catalog={simulation.protocolCatalog}
                        canSubmit={formalCanSubmit}
                        submissionReason={formalCanSubmit ? "完整 MD 模拟容量可用" : servicePresentation.title}
                        isSubmitting={simulation.isSubmitting}
                        templateChangeCount={templateChanges.size}
                        onProtocolChange={setSelectedProtocol}
                        onApplyConfig={(protocol, config) => setConfigs((current) => ({ ...current, [protocol]: config }))}
                        onRestoreTemplate={restoreProtocolTemplate}
                        onKeepChangedTemplates={keepChangedTemplates}
                        onRestoreChangedTemplates={restoreChangedTemplates}
                        onSubmit={() => void submitFormal()}
                      />
                    )}
                    {simulation.error ? <div className="np-mmd-inline-error" role="alert">{translateMonomerMdMessage(simulation.error)}</div> : null}
                  </div>
                ) : null}

                {activeTab === "tasks" ? (
                  <MonomerMdTaskCenter
                    selectedJob={simulation.job}
                    activeJobs={simulation.activeJobs}
                    isActiveJobsLoading={simulation.isActiveJobsLoading}
                    activeJobsError={simulation.activeJobsError}
                    history={simulation.history}
                    historyQuery={simulation.historyQuery}
                    isHistoryLoading={simulation.isHistoryLoading}
                    historyError={simulation.historyError}
                    cancellingJobIds={simulation.cancellingJobIds}
                    deletingJobIds={simulation.deletingJobIds}
                    deleteJobErrors={simulation.deleteJobErrors}
                    onRefresh={() => { void simulation.refreshActiveJobs(); void simulation.refreshHistory(); }}
                    onSelect={(job) => { setActiveTab("results"); void simulation.selectJob(job.job_id); }}
                    onCancel={(job) => { if (window.confirm("确定取消这个全局正式任务吗？")) void simulation.cancelJob(job); }}
                    onDelete={(job) => { if (window.confirm("删除后任务记录、结果和深链均无法恢复。确定继续吗？")) void simulation.deleteJobRecord(job); }}
                    onChangeQuery={simulation.changeHistoryQuery}
                  />
                ) : null}

                {activeTab === "results" ? (
                  <MonomerMdResultsPanel
                    job={simulation.job}
                    result={simulation.data}
                    isLoading={simulation.isJobLoading}
                    error={simulation.error}
                    cancelling={Boolean(simulation.job && simulation.cancellingJobIds.includes(simulation.job.job_id))}
                    deleting={Boolean(simulation.job && simulation.deletingJobIds.includes(simulation.job.job_id))}
                    onCancel={(job) => void simulation.cancelJob(job)}
                    onDelete={(job) => void simulation.deleteJobRecord(job)}
                    onClear={() => simulation.clearSelectedJob()}
                  />
                ) : null}
              </section>
            </main>
          </div>
        </div>
      </div>
    </div>
  );
}
