import { BrowsingRecordingControls } from "./browsing-recording/BrowsingRecording";
import { ModulePageHeader } from "./ModulePageHeader";
import { useContentMotion } from "../hooks/useContentMotion";
import {
  Activity,
  Atom,
  CircleOff,
  FlaskConical,
  Info,
  LoaderCircle,
  RefreshCw,
  SlidersHorizontal,
  TriangleAlert,
} from "lucide-react";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useMdSimulationDemo } from "../hooks/useMdSimulationDemo";
import type { MdDemoRunRequest, StructureWorkspaceContext } from "../types";
import "../styles/structure-workbench.css";
import "../styles/md-simulation.css";
import {
  MD_DEMO_EXAMPLE_SMILES,
  MD_DEMO_FALLBACK_REQUEST,
  normalizeMdDemoRequest,
  sameMdDemoRequest,
  validateMdDemoRequest,
  type MdSimulationField,
  type MdSimulationFormErrors,
} from "./md-simulation/config";
import { MdSimulationParameters } from "./md-simulation/MdSimulationParameters";
import { MdSimulationResultsPanel } from "./md-simulation/MdSimulationResultsPanel";
import { MdStructureInput } from "./md-simulation/MdStructureInput";
import {
  clearMdSimulationDraft,
  readMdSimulationDraft,
  saveMdSimulationDraft,
} from "./md-simulation/session";

type MdSimulationDemoPageProps = {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
  resultRevealDelayMs?: number;
};

type TouchedState = Partial<Record<MdSimulationField, boolean>>;
type WorkspaceTab = "input" | "results";

function withExampleSmiles(request: MdDemoRunRequest): MdDemoRunRequest {
  return { ...request, smiles: MD_DEMO_EXAMPLE_SMILES };
}

function fillExampleSmiles(request: MdDemoRunRequest): MdDemoRunRequest {
  const restoredSmiles = request.smiles.trim();
  return {
    ...request,
    smiles:
      restoredSmiles && restoredSmiles.toUpperCase() !== "CC"
        ? request.smiles
        : MD_DEMO_EXAMPLE_SMILES,
    forcefield: request.forcefield.trim()
      ? request.forcefield
      : MD_DEMO_FALLBACK_REQUEST.forcefield,
  };
}

function SectionHeader({
  index,
  title,
  description,
}: {
  index: string;
  title: string;
  description: string;
}) {
  return (
    <header className="np-md-section__header">
      <span>{index}</span>
      <div>
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
    </header>
  );
}

function WorkspaceTabs({
  activeTab,
  hasAttempt,
  loading,
  onChange,
}: {
  activeTab: WorkspaceTab;
  hasAttempt: boolean;
  loading: boolean;
  onChange: (tab: WorkspaceTab) => void;
}) {
  return (
    <div className="np-md-workspace-navigation">
      <div
        className="np-md-workspace-tabs"
        role="tablist"
        aria-label="MD 模拟工作区"
      >
        <button
          id="md-simulation-input-tab"
          type="button"
          role="tab"
          aria-selected={activeTab === "input"}
          aria-controls="md-simulation-input-panel"
          className={activeTab === "input" ? "is-active" : ""}
          onClick={() => onChange("input")}
        >
          <Atom aria-hidden="true" />
          结构输入
        </button>
        <button
          id="md-simulation-results-tab"
          type="button"
          role="tab"
          aria-selected={activeTab === "results"}
          aria-controls="md-simulation-results-panel"
          className={activeTab === "results" ? "is-active" : ""}
          onClick={() => onChange("results")}
        >
          <Activity aria-hidden="true" />
          模拟结果
          {loading ? (
            <small>运行中</small>
          ) : hasAttempt ? (
            <small>已完成</small>
          ) : null}
        </button>
      </div>
    </div>
  );
}

export function MdSimulationDemoPage({
  structure,
  onEditStructure,
  resultRevealDelayMs,
}: MdSimulationDemoPageProps) {
  const [initial] = useState(() => {
    const draft = readMdSimulationDraft();
    return {
      request: draft
        ? fillExampleSmiles(draft)
        : withExampleSmiles(MD_DEMO_FALLBACK_REQUEST),
      hadDraft: Boolean(draft),
    };
  });
  const [request, setRequest] = useState<MdDemoRunRequest>(initial.request);
  const [touched, setTouched] = useState<TouchedState>({});
  const [submitAttempted, setSubmitAttempted] = useState(false);
  const [hasAttempt, setHasAttempt] = useState(false);
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>("input");
  const tabContentRef = useRef<HTMLDivElement | null>(null);
  useContentMotion(tabContentRef, workspaceTab, "tab");
  const [parametersOpen, setParametersOpen] = useState(false);
  const [attemptSnapshot, setAttemptSnapshot] =
    useState<MdDemoRunRequest | null>(null);
  const [resultSnapshot, setResultSnapshot] = useState<MdDemoRunRequest | null>(
    null,
  );
  const requestRef = useRef(request);
  const formDirtyRef = useRef(initial.hadDraft);
  const persistDraftRef = useRef(initial.hadDraft);
  const defaultsAppliedRef = useRef(initial.hadDraft);
  const parameterPanelRef = useRef<HTMLElement | null>(null);
  const parameterButtonRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocusFrameRef = useRef<number | null>(null);
  requestRef.current = request;
  const simulation = useMdSimulationDemo({ resultRevealDelayMs });

  const closeParameters = useCallback((restoreFocus = true) => {
    setParametersOpen(false);
    if (!restoreFocus) return;
    if (restoreFocusFrameRef.current !== null)
      window.cancelAnimationFrame(restoreFocusFrameRef.current);
    restoreFocusFrameRef.current = window.requestAnimationFrame(() => {
      restoreFocusFrameRef.current = null;
      parameterButtonRef.current?.focus();
    });
  }, []);

  useEffect(() => {
    if (!parametersOpen) return;
    const frame = window.requestAnimationFrame(() => {
      parameterPanelRef.current
        ?.querySelector<HTMLElement>("input, button")
        ?.focus();
    });
    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (
        !parameterPanelRef.current?.contains(target) &&
        !parameterButtonRef.current?.contains(target)
      ) {
        closeParameters(false);
      }
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeParameters(true);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [closeParameters, parametersOpen]);

  useEffect(() => {
    return () => {
      if (restoreFocusFrameRef.current !== null)
        window.cancelAnimationFrame(restoreFocusFrameRef.current);
    };
  }, []);

  const updateRequest = useCallback((update: Partial<MdDemoRunRequest>) => {
    formDirtyRef.current = true;
    persistDraftRef.current = true;
    setRequest((current) => ({ ...current, ...update }));
  }, []);

  useEffect(() => {
    const defaults = simulation.defaults?.default_request;
    if (!defaults || defaultsAppliedRef.current || formDirtyRef.current) return;
    defaultsAppliedRef.current = true;
    setRequest(withExampleSmiles(defaults));
  }, [simulation.defaults]);

  useEffect(() => {
    if (persistDraftRef.current) saveMdSimulationDraft(request);
  }, [request]);

  useEffect(() => {
    return () => {
      if (persistDraftRef.current) saveMdSimulationDraft(requestRef.current);
    };
  }, []);

  const validationErrors = useMemo(
    () => validateMdDemoRequest(request),
    [request],
  );
  const visibleErrors = useMemo(() => {
    const errors: MdSimulationFormErrors = {};
    (Object.keys(validationErrors) as MdSimulationField[]).forEach((field) => {
      if (submitAttempted || touched[field])
        errors[field] = validationErrors[field];
    });
    return errors;
  }, [submitAttempted, touched, validationErrors]);
  const isReady = Boolean(
    simulation.defaults &&
    !simulation.defaultsLoading &&
    !simulation.defaultsError,
  );
  const canRun =
    isReady &&
    !simulation.runLoading &&
    Object.keys(validationErrors).length === 0;
  const stale = Boolean(
    simulation.data &&
    resultSnapshot &&
    !sameMdDemoRequest(resultSnapshot, request),
  );

  const serviceState = simulation.defaultsLoading
    ? "loading"
    : simulation.defaultsError
      ? "error"
      : simulation.defaults
        ? "ready"
        : "unavailable";
  const serviceLabel =
    serviceState === "loading"
      ? "检查中"
      : serviceState === "ready"
        ? "准备就绪"
        : serviceState === "error"
          ? "读取失败"
          : "不可用";
  const parameterStatus = simulation.defaultsLoading
    ? "正在加载模拟配置"
    : simulation.defaultsError
      ? "请刷新后重试"
      : validationErrors.smiles
        ? "请先完善结构输入"
        : Object.keys(validationErrors).length
          ? "请检查标红的参数"
          : "参数已就绪";

  const getSharedSmiles = useCallback(async () => {
    let sharedSmiles = structure.smiles.trim();
    try {
      const current = (await structure.getCurrentSmiles()).trim();
      if (current) sharedSmiles = current;
    } catch (error) {
      console.warn("Failed to read current structure from workbench", error);
    }
    if (!sharedSmiles)
      throw new Error("共享结构为空，请先在结构工作台绘制或输入结构。");
    return sharedSmiles;
  }, [structure]);

  function openStructureEditor() {
    saveMdSimulationDraft(requestRef.current);
    persistDraftRef.current = true;
    closeParameters(false);
    onEditStructure();
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitAttempted(true);
    if (!canRun) {
      if (validationErrors.smiles) setWorkspaceTab("input");
      return;
    }
    const snapshot = normalizeMdDemoRequest(request);
    setRequest(snapshot);
    setAttemptSnapshot(snapshot);
    setWorkspaceTab("results");
    closeParameters(false);
    const result = await simulation.run(snapshot);
    if (result) {
      setHasAttempt(true);
      setResultSnapshot(normalizeMdDemoRequest(result.input));
    }
  }

  function resetForm() {
    const defaults =
      simulation.defaults?.default_request ?? MD_DEMO_FALLBACK_REQUEST;
    persistDraftRef.current = false;
    formDirtyRef.current = false;
    defaultsAppliedRef.current = true;
    clearMdSimulationDraft();
    simulation.clearResults();
    setRequest(withExampleSmiles(defaults));
    setTouched({});
    setSubmitAttempted(false);
    setHasAttempt(false);
    setWorkspaceTab("input");
    setParametersOpen(false);
    setAttemptSnapshot(null);
    setResultSnapshot(null);
  }

  return (
    <div
      className="np-module-page np-structure-workbench np-md-simulation"
      data-module="md-simulation"
    >
      <ModulePageHeader actions={<BrowsingRecordingControls module="mdSimulationDemo" />}>MD 模拟</ModulePageHeader>
      <div className="np-sw-page np-module-page-body np-md-page">
        <div className="np-sw-layout">
          <main className="np-sw-workspace">
            <div className="np-md-module-toolbar" aria-label="MD 模拟工具栏">
              <div className="np-md-toolbar-actions">
                <div className="np-md-service-status">
                  <span className={`is-${serviceState}`} role="status">
                    {serviceState === "loading" ? (
                      <LoaderCircle className="np-sw-spin" />
                    ) : null}
                    {serviceState === "ready" ? (
                      <i className="np-md-ready-dot" aria-hidden="true" />
                    ) : null}
                    {serviceState === "unavailable" ? <CircleOff /> : null}
                    {serviceState === "error" ? <TriangleAlert /> : null}
                    {serviceLabel}
                  </span>
                  <button
                    type="button"
                    aria-label="重新加载 MD 模拟配置"
                    disabled={simulation.defaultsLoading}
                    onClick={() => void simulation.refreshDefaults()}
                  >
                    <RefreshCw
                      className={simulation.defaultsLoading ? "np-sw-spin" : ""}
                    />
                    刷新
                  </button>
                </div>
                <button
                  ref={parameterButtonRef}
                  type="button"
                  className={`np-sw-tool np-md-parameter-trigger${parametersOpen ? " is-active" : ""}`}
                  aria-expanded={parametersOpen}
                  aria-controls="md-simulation-parameters"
                  onClick={() => setParametersOpen((current) => !current)}
                >
                  <SlidersHorizontal aria-hidden="true" />
                  参数设置
                </button>
              </div>
            </div>

            <div className="np-md-scroll-region">
              <div className="np-md-content-column">
                <section
                  className="np-md-workbench-surface np-sw-accented-surface"
                  aria-label="MD 模拟主工作区"
                >
                  <div ref={tabContentRef} className="np-md-workspace-view">
                    {workspaceTab === "input" ? (
                      <section
                        id="md-simulation-input-panel"
                        className="np-md-surface"
                        role="tabpanel"
                        aria-labelledby="md-simulation-input-tab"
                      >
                        <header className="np-md-view-header np-md-surface__header">
                          <div className="np-md-view-heading np-md-surface-heading">
                            <span className="np-md-surface-mark">
                              <FlaskConical />
                            </span>
                            <div>
                              <h2>聚合物结构输入</h2>
                              <p>已填入真实示例结构，也可以导入全局共享结构。</p>
                            </div>
                          </div>
                          <span className="np-md-view-badge">
                            <Info />
                            真实示例结构
                          </span>
                        </header>

                        <WorkspaceTabs
                          activeTab={workspaceTab}
                          hasAttempt={hasAttempt}
                          loading={simulation.runLoading}
                          onChange={setWorkspaceTab}
                        />

                        <div className="np-md-demo-boundary">
                          <Info aria-hidden="true" />
                          <span>
                            本模块以示例 SMILES 展示 MD
                            模拟流程，结果来自该示例结构已完成的真实 MD 计算。
                          </span>
                        </div>

                        {simulation.defaultsError ? (
                          <div className="np-md-service-message" role="alert">
                            <TriangleAlert />
                            {simulation.defaultsError}
                          </div>
                        ) : null}

                        <section className="np-md-section">
                          <SectionHeader
                            index="01"
                            title="聚合物结构"
                            description="示例 SMILES 已预置，可直接打开右上角参数设置开始模拟。"
                          />
                          <MdStructureInput
                            value={request.smiles}
                            error={visibleErrors.smiles ?? null}
                            onChange={(smiles) => updateRequest({ smiles })}
                            onTouched={() =>
                              setTouched((current) => ({
                                ...current,
                                smiles: true,
                              }))
                            }
                            getSharedSmiles={getSharedSmiles}
                            onEditStructure={openStructureEditor}
                          />
                        </section>

                        <footer className="np-md-input-footer">
                          <div>
                            <strong>示例结构已就绪</strong>
                            <span>通过右上角“参数设置”检查条件并开始模拟。</span>
                          </div>
                          <code>{MD_DEMO_EXAMPLE_SMILES}</code>
                        </footer>
                      </section>
                    ) : (
                      <MdSimulationResultsPanel
                        workspaceNavigation={
                          <WorkspaceTabs
                            activeTab={workspaceTab}
                            hasAttempt={hasAttempt}
                            loading={simulation.runLoading}
                            onChange={setWorkspaceTab}
                          />
                        }
                        loading={simulation.runLoading}
                        error={simulation.runError}
                        progress={simulation.progress}
                        data={simulation.data}
                        attemptSnapshot={attemptSnapshot}
                        resultSnapshot={resultSnapshot}
                        stale={stale}
                        distance={simulation.distance}
                        distanceLoading={simulation.distanceLoading}
                        distanceError={simulation.distanceError}
                        onShowInput={() => setWorkspaceTab("input")}
                        onSelectionChange={simulation.clearDistance}
                        onCalculateDistance={(atomId1, atomId2) =>
                          void simulation.calculateDistance({
                            atom_id_1: atomId1,
                            atom_id_2: atomId2,
                            use_pbc: true,
                          })
                        }
                      />
                    )}
                  </div>
                </section>
              </div>
            </div>

            <MdSimulationParameters
              open={parametersOpen}
              panelRef={parameterPanelRef}
              request={request}
              errors={visibleErrors}
              canRun={canRun}
              submitting={simulation.runLoading}
              statusMessage={parameterStatus}
              onChange={updateRequest}
              onTouched={(field) =>
                setTouched((current) => ({ ...current, [field]: true }))
              }
              onClose={closeParameters}
              onSubmit={(event) => void submit(event)}
              onReset={resetForm}
            />
          </main>
        </div>
      </div>
    </div>
  );
}
