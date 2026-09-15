import { BrowsingRecordingControls } from "./browsing-recording/BrowsingRecording";
import { ModulePageHeader } from "./ModulePageHeader";
import {
  CircleOff,
  FlaskConical,
  LoaderCircle,
  Play,
  RefreshCw,
  RotateCcw,
  TriangleAlert
} from "lucide-react";
import {
  type CSSProperties,
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import { useMonomerPolymerization } from "../hooks/useMonomerPolymerization";
import type {
  MonomerPolymerizationRequest,
  MonomerPolymerizationTargetClass,
  StructureWorkspaceContext
} from "../types";
import "../styles/structure-workbench.css";
import "../styles/monomer-polymerization.css";
import {
  clampInteger,
  DEFAULT_TARGET_CLASSES,
  getTargetRequirement,
  SMIPOLY_POLYIMIDE_FIXTURE,
  TARGET_CLASS_LABELS
} from "./monomer-polymerization/config";
import {
  MonomerPairEditor,
  type MonomerSlot
} from "./monomer-polymerization/MonomerPairEditor";
import { PolymerClassPicker } from "./monomer-polymerization/PolymerClassPicker";
import {
  MonomerPolymerizationDrawer,
  type MonomerPolymerizationSnapshot
} from "./monomer-polymerization/MonomerPolymerizationDrawer";
import {
  clearMonomerPolymerizationDraft,
  readMonomerPolymerizationDraft,
  saveMonomerPolymerizationDraft,
  type MonomerPolymerizationDraft
} from "./monomer-polymerization/session";

import { BatchPolymerizationPanel, type BatchPolymerizationSections } from "./monomer-polymerization/BatchPolymerizationPanel";

export { SMIPOLY_POLYIMIDE_FIXTURE };

type MonomerPolymerizationPageProps = {
  structure: StructureWorkspaceContext;
  onEditStructure: () => void;
};

type FormTouchedState = {
  A: boolean;
  B: boolean;
  maxResults: boolean;
};

const DEFAULT_MAX_RESULTS = 10;
const NATIVE_2K_QUERY = "(min-width: 2000px) and (min-height: 1120px)";
function isNative2KViewport() {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(NATIVE_2K_QUERY).matches
    : false;
}
function defaultForm(sharedSmiles: string): MonomerPolymerizationDraft {
  return {
    monomerA: sharedSmiles.trim(),
    monomerB: "",
    targetClass: "polyimide",
    maxResults: DEFAULT_MAX_RESULTS
  };
}

function sameSnapshot(
  snapshot: MonomerPolymerizationSnapshot | null,
  request: MonomerPolymerizationRequest
) {
  return Boolean(
    snapshot &&
      snapshot.monomer_a_smiles === request.monomer_a_smiles &&
      snapshot.monomer_b_smiles === request.monomer_b_smiles &&
      snapshot.target_class === request.target_class &&
      snapshot.max_results === request.max_results
  );
}

export function MonomerPolymerizationPage({
  structure,
  onEditStructure
}: MonomerPolymerizationPageProps) {
  const [initialForm] = useState<MonomerPolymerizationDraft>(() =>
    readMonomerPolymerizationDraft() ?? defaultForm(structure.smiles)
  );
  const [form, setForm] = useState(initialForm);
  const [touched, setTouched] = useState<FormTouchedState>({
    A: false,
    B: false,
    maxResults: false
  });
  const [submitAttempted, setSubmitAttempted] = useState(false);
  const [hasAttempt, setHasAttempt] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [native2K, setNative2K] = useState(isNative2KViewport);
  const [drawerWidth, setDrawerWidth] = useState(() => isNative2KViewport() ? 540 : 380);
  const [snapshot, setSnapshot] = useState<MonomerPolymerizationSnapshot | null>(null);
  const [editorRevision, setEditorRevision] = useState(0);
  const persistDraftRef = useRef(false);
  const formRef = useRef(form);
  formRef.current = form;
  const polymerization = useMonomerPolymerization();
  const [mode, setMode] = useState<"batch" | "single" | null>(() => {
    const value = new URLSearchParams(window.location.search).get("mode");
    return value === "batch" || value === "single" ? value : null;
  });
  // A failed status refresh must not unmount the batch form and lose selected files.
  const lastBatchEnabled = useRef(false);
  if (polymerization.status?.batch) lastBatchEnabled.current = polymerization.status.batch.enabled;
  const batchEnabled = lastBatchEnabled.current;
  const effectiveMode = mode ?? (batchEnabled ? "batch" : "single");
  const isBatch = effectiveMode === "batch";
  const showModeSwitcher = batchEnabled || isBatch;
  useEffect(() => {
    const update = () => {
      const value = new URLSearchParams(window.location.search).get("mode");
      setMode(value === "batch" || value === "single" ? value : null);
    };
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);


  const updateForm = useCallback((update: Partial<MonomerPolymerizationDraft>) => {
    persistDraftRef.current = true;
    setForm((current) => ({ ...current, ...update }));
  }, []);

  useEffect(() => {
    if (persistDraftRef.current) saveMonomerPolymerizationDraft(form);
  }, [form]);

  useEffect(() => {
    return () => {
      if (persistDraftRef.current) saveMonomerPolymerizationDraft(formRef.current);
    };
  }, []);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(NATIVE_2K_QUERY);
    const handleChange = (event: MediaQueryListEvent) => {
      setNative2K(event.matches);
      setDrawerWidth((current) => event.matches
        ? Math.min(720, Math.max(480, current === 380 ? 540 : current))
        : Math.min(560, Math.max(320, current))
      );
    };
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, []);

  const targetOptions = useMemo(() => {
    const available = polymerization.status?.available_target_classes;
    if (!available?.length) return DEFAULT_TARGET_CLASSES;
    const filtered = DEFAULT_TARGET_CLASSES.filter((target) => available.includes(target));
    return filtered.length ? filtered : DEFAULT_TARGET_CLASSES;
  }, [polymerization.status]);

  const maxResultsLimit = Math.max(1, polymerization.status?.max_results_limit ?? 20);

  useEffect(() => {
    const status = polymerization.status;
    if (!status) return;
    setForm((current) => {
      const nextTarget = targetOptions.includes(current.targetClass)
        ? current.targetClass
        : targetOptions.includes(status.default_target_class)
          ? status.default_target_class
          : targetOptions[0];
      const nextMaxResults = clampInteger(current.maxResults, 1, maxResultsLimit);
      if (nextTarget === current.targetClass && nextMaxResults === current.maxResults) return current;
      return { ...current, targetClass: nextTarget, maxResults: nextMaxResults };
    });
  }, [maxResultsLimit, polymerization.status, targetOptions]);

  const targetRequirement = getTargetRequirement(form.targetClass, polymerization.status);
  const targetPickerOptions = useMemo(() => targetOptions.map((target) => {
    const requirement = getTargetRequirement(target, polymerization.status);
    const monomerCount = requirement.min_monomers === requirement.max_monomers
      ? `${requirement.min_monomers} 个单体`
      : `${requirement.min_monomers}–${requirement.max_monomers} 个单体`;
    return {
      value: target,
      label: TARGET_CLASS_LABELS[target],
      monomerCount,
      monomerBRequired: requirement.monomer_b_required
    };
  }), [polymerization.status, targetOptions]);
  const targetMonomerCount = targetRequirement.min_monomers === targetRequirement.max_monomers
    ? `${targetRequirement.min_monomers} 个单体`
    : `${targetRequirement.min_monomers}–${targetRequirement.max_monomers} 个单体`;
  const monomerAValue = form.monomerA.trim();
  const monomerBValue = form.monomerB.trim();
  const monomerAHasDummyAtom = monomerAValue.includes("*");
  const monomerBHasDummyAtom = monomerBValue.includes("*");
  const showMonomerARequired = (touched.A || submitAttempted) && !monomerAValue;
  const showMonomerBRequired =
    (touched.B || submitAttempted) && targetRequirement.monomer_b_required && !monomerBValue;
  const monomerAError = showMonomerARequired
    ? "请输入单体 A 的 SMILES。"
    : monomerAHasDummyAtom
      ? "普通单体 SMILES 不应包含 * 连接点。"
      : null;
  const monomerBError = showMonomerBRequired
    ? `${TARGET_CLASS_LABELS[form.targetClass]} 需要单体 B。`
    : monomerBHasDummyAtom
      ? "普通单体 SMILES 不应包含 * 连接点。"
      : null;
  const maxResultsValid =
    Number.isInteger(form.maxResults) && form.maxResults >= 1 && form.maxResults <= maxResultsLimit;
  const maxResultsError = (touched.maxResults || submitAttempted) && !maxResultsValid
    ? `返回数量需为 1–${maxResultsLimit} 的整数。`
    : null;
  const serviceReady = Boolean(
    !polymerization.statusLoading &&
      polymerization.status?.enabled &&
      polymerization.status.available
  );
  const inputValid = Boolean(
    monomerAValue &&
      !monomerAHasDummyAtom &&
      (!targetRequirement.monomer_b_required || monomerBValue) &&
      !monomerBHasDummyAtom &&
      maxResultsValid
  );
  const canSubmit = serviceReady && inputValid && !polymerization.runLoading;

  const currentRequest = useMemo<MonomerPolymerizationRequest>(() => ({
    monomer_a_smiles: monomerAValue,
    monomer_b_smiles: monomerBValue || null,
    target_class: form.targetClass,
    max_results: form.maxResults
  }), [form.maxResults, form.targetClass, monomerAValue, monomerBValue]);
  const stale = hasAttempt && !sameSnapshot(snapshot, currentRequest);

  const getSharedSmiles = useCallback(async () => {
    let sharedSmiles = structure.smiles.trim();
    try {
      const currentSmiles = (await structure.getCurrentSmiles()).trim();
      if (currentSmiles) sharedSmiles = currentSmiles;
    } catch (error) {
      console.warn("Failed to read current structure from workbench", error);
    }
    if (!sharedSmiles) {
      throw new Error("共享结构为空，请先在结构工作台绘制或输入单体。");
    }
    return sharedSmiles;
  }, [structure]);

  function touchSlot(slot: MonomerSlot) {
    setTouched((current) => ({ ...current, [slot]: true }));
  }

  function editSharedStructure() {
    persistDraftRef.current = true;
    saveMonomerPolymerizationDraft(formRef.current);
    onEditStructure();
  }

  function submitPolymerization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isBatch) return;
    setSubmitAttempted(true);
    if (!canSubmit) return;
    const request: MonomerPolymerizationRequest = {
      monomer_a_smiles: monomerAValue,
      monomer_b_smiles: monomerBValue || null,
      target_class: form.targetClass,
      max_results: form.maxResults
    };
    persistDraftRef.current = true;
    saveMonomerPolymerizationDraft(formRef.current);
    setSnapshot(request);
    setHasAttempt(true);
    setDrawerOpen(true);
    void polymerization.run(request);
  }

  function clearResults() {
    polymerization.clearResults();
    setSnapshot(null);
    setHasAttempt(false);
    setDrawerOpen(false);
  }

  function resetForm() {
    const resetTarget: MonomerPolymerizationTargetClass = targetOptions.includes("polyimide")
      ? "polyimide"
      : polymerization.status && targetOptions.includes(polymerization.status.default_target_class)
        ? polymerization.status.default_target_class
        : targetOptions[0];
    persistDraftRef.current = false;
    clearMonomerPolymerizationDraft();
    polymerization.clearResults();
    setForm({
      monomerA: structure.smiles.trim(),
      monomerB: "",
      targetClass: resetTarget,
      maxResults: Math.min(DEFAULT_MAX_RESULTS, maxResultsLimit)
    });
    setTouched({ A: false, B: false, maxResults: false });
    setSubmitAttempted(false);
    setSnapshot(null);
    setHasAttempt(false);
    setDrawerOpen(false);
    setEditorRevision((current) => current + 1);
  }

  const serviceState = polymerization.statusLoading
    ? "loading"
    : polymerization.statusError
      ? "error"
      : polymerization.status?.enabled && polymerization.status.available
        ? "ready"
        : "unavailable";
  const serviceLabel = serviceState === "loading"
    ? "检查中"
    : serviceState === "ready"
      ? "准备就绪"
      : serviceState === "error"
        ? "检查失败"
        : "不可用";
  const drawerSizing = native2K
    ? { minWidth: 480, maxWidth: 720, keyboardStep: 24 }
    : { minWidth: 320, maxWidth: 560, keyboardStep: 16 };
  const workbenchStyle = { "--np-sw-drawer-width": `${drawerWidth}px` } as CSSProperties;

  function selectMode(value: "batch" | "single") {
    setMode(value);
    const url = new URL(window.location.href);
    url.searchParams.set("mode", value);
    window.history.replaceState(window.history.state, "", url);
  }
  const modeSwitcher = (
    <div className="np-mp-input-tabs" role="tablist" aria-label="聚合模式"
      onKeyDown={(event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? "batch" : event.key === "End" ? "single" : isBatch ? "single" : "batch";
        selectMode(next);
        event.currentTarget.querySelector<HTMLButtonElement>(`#np-mp-${next}-tab`)?.focus();
      }}
    >
      {(["batch", "single"] as const).map((value) => (
        <button key={value} id={`np-mp-${value}-tab`} type="button" role="tab"
          aria-selected={effectiveMode === value} aria-controls={`np-mp-${value}-panel`}
          tabIndex={effectiveMode === value ? 0 : -1} onClick={() => selectMode(value)}
        >{value === "batch" ? "批量聚合" : "单次聚合"}</button>
      ))}
    </div>
  );
  const drawerVisible = !isBatch && drawerOpen;

  const renderWorkbench = (batch?: BatchPolymerizationSections) => (
    <div
      className="np-module-page np-structure-workbench np-monomer-polymerization"
      data-module="monomer-polymerization"
      style={workbenchStyle}
    >
      <ModulePageHeader actions={<BrowsingRecordingControls module="monomerPolymerization" />}>单体正向聚合</ModulePageHeader>
      <div className={`np-sw-page np-module-page-body np-mp-page${drawerVisible ? " has-open-drawer" : ""}`}>
        <div className={`np-sw-layout${drawerVisible ? " has-open-drawer" : ""}`}>
          <main className="np-sw-workspace">
            <div className="np-mp-content">
              <div className="np-mp-module-toolbar" aria-label="单体正向聚合状态">
                <div className="np-mp-service-status">
                  <span className={`is-${serviceState}`} role="status">
                    {serviceState === "loading" ? <LoaderCircle className="np-sw-spin" /> : null}
                    {serviceState === "ready" ? <i className="np-mp-ready-dot" aria-hidden="true" /> : null}
                    {serviceState === "unavailable" ? <CircleOff /> : null}
                    {serviceState === "error" ? <TriangleAlert /> : null}
                    {serviceLabel}
                  </span>
                  <button
                    type="button"
                    aria-label="重新检查 SMiPoly 是否可用"
                    onClick={() => void polymerization.refreshStatus()}
                    disabled={polymerization.statusLoading}
                  >
                    <RefreshCw aria-hidden="true" />
                    刷新
                  </button>
                </div>
              </div>

              <form
                className="np-mp-surface np-sw-accented-surface"
                onSubmit={submitPolymerization}
                noValidate
              >
                <header className="np-mp-surface__header">
                  <div className="np-mp-surface-heading">
                    <span className="np-mp-surface-mark"><FlaskConical aria-hidden="true" /></span>
                    <div className="np-mp-surface-copy">
                      <h2>正向聚合设置</h2>
                      <p>选择目标类型并输入单体，生成聚合物候选。</p>
                    </div>
                  </div>
                </header>

                {polymerization.statusError || serviceState === "unavailable" ? (
                  <div className="np-mp-service-message" role="alert">
                    <TriangleAlert aria-hidden="true" />
                    <span>
                      {polymerization.statusError ?? (
                        polymerization.status?.enabled === false
                          ? "单体正向聚合功能当前未启用。"
                          : "SMiPoly 当前不可用，请稍后刷新重试。"
                      )}
                    </span>
                  </div>
                ) : null}

                <section className="np-mp-section" aria-labelledby="np-mp-target-title">
                  <header className="np-mp-section__header">
                    <span>01</span>
                    <div>
                      <h2 id="np-mp-target-title">目标聚合物类型</h2>
                      <p>不同聚合物类型需要的单体数量可能不同。</p>
                    </div>
                  </header>
                  <div className="np-mp-target-grid">
                    <PolymerClassPicker
                      value={form.targetClass}
                      options={targetPickerOptions}
                      onChange={(targetClass) => {
                        updateForm({ targetClass });
                        setSubmitAttempted(false);
                      }}
                    />
                    <div className="np-mp-field np-mp-requirement-summary">
                      <span>MONOMER REQUIREMENT</span>
                      <div className="np-mp-requirement-card">
                        <div className="np-mp-requirement-card__metric">
                          <span>所需单体</span>
                          <strong>{targetMonomerCount}</strong>
                        </div>
                        <div className="np-mp-requirement-card__copy">
                          <span>{targetRequirement.monomer_b_required ? "B 必填" : "B 可选"}</span>
                          <p>{targetRequirement.note}</p>
                        </div>
                      </div>
                    </div>
                  </div>
                </section>

                <section className="np-mp-section" aria-labelledby="np-mp-monomers-title">
                  <header className="np-mp-section__header">
                    <span>02</span>
                    <div>
                      <h2 id="np-mp-monomers-title">单体输入</h2>
                      <p>{isBatch ? "批量需上传单体表 A 和 B，预检后计算两表的全部组合。" : "填写单体 A 和 B，可导入共享结构并查看 2D 预览。"}</p>
                    </div>
                  </header>
                  {showModeSwitcher ? modeSwitcher : null}
                  <div id="np-mp-batch-panel" role={showModeSwitcher ? "tabpanel" : undefined}
                    aria-labelledby={showModeSwitcher ? "np-mp-batch-tab" : undefined} hidden={!isBatch}>
                    {batch?.inputs}
                  </div>
                  <div id="np-mp-single-panel" role={showModeSwitcher ? "tabpanel" : undefined}
                    aria-labelledby={showModeSwitcher ? "np-mp-single-tab" : undefined} hidden={isBatch}>
                    <MonomerPairEditor
                      key={editorRevision}
                      monomerA={form.monomerA}
                      monomerB={form.monomerB}
                      monomerBRequired={targetRequirement.monomer_b_required}
                      monomerBRequirementNote={targetRequirement.note}
                      monomerAError={monomerAError}
                      monomerBError={monomerBError}
                      onMonomerAChange={(monomerA) => updateForm({ monomerA })}
                      onMonomerBChange={(monomerB) => updateForm({ monomerB })}
                      onTouched={touchSlot}
                      getSharedSmiles={getSharedSmiles}
                      onEditStructure={editSharedStructure}
                    />
                  </div>
                </section>

                <section className="np-mp-section" aria-labelledby="np-mp-settings-title">
                  <header className="np-mp-section__header">
                    <span>03</span>
                    <div>
                      <h2 id="np-mp-settings-title">运行设置</h2>
                      <p>确认设置后开始聚合，查看生成的候选结果。</p>
                    </div>
                  </header>
                  <div hidden={!isBatch}>{batch?.settings}</div>
                  <div hidden={isBatch}>
                    <div className="np-mp-run-grid">
                      <label className="np-mp-field">
                        <span>MAX RESULTS</span>
                        <input
                          type="number"
                          min={1}
                          max={maxResultsLimit}
                          step={1}
                          value={form.maxResults}
                          aria-invalid={maxResultsError ? true : undefined}
                          aria-describedby={maxResultsError ? "np-mp-max-results-error" : "np-mp-max-results-hint"}
                          onChange={(event) => updateForm({
                            maxResults: event.target.value === "" ? 0 : Number(event.target.value)
                          })}
                          onBlur={() => setTouched((current) => ({ ...current, maxResults: true }))}
                        />
                        <small id="np-mp-max-results-hint">最多可返回：{maxResultsLimit}</small>
                        {maxResultsError ? (
                          <small id="np-mp-max-results-error" className="np-mp-field-error" role="alert">
                            {maxResultsError}
                          </small>
                        ) : null}
                      </label>
                      <div className="np-mp-run-note">
                        <FlaskConical aria-hidden="true" />
                        <p>生成的候选不代表一定可以合成，也不代表相关性质已经得到验证；请结合实验条件进一步评估。</p>
                      </div>
                    </div>

                    <div className="np-mp-form-actions">
                      <button type="submit" className="np-sw-primary-button" disabled={isBatch || !canSubmit}>
                        {polymerization.runLoading ? <LoaderCircle className="np-sw-spin" /> : <Play />}
                        {polymerization.runLoading ? "聚合中" : "聚合"}
                      </button>
                      <button type="button" className="np-sw-secondary-button" onClick={resetForm}>
                        <RotateCcw aria-hidden="true" />
                        重置
                      </button>
                    </div>
                  </div>
                </section>
              </form>
              <div hidden={!isBatch}>{batch?.taskPanel}</div>
            </div>
          </main>

          {!isBatch ? <MonomerPolymerizationDrawer
            open={drawerVisible}
            hasAttempt={hasAttempt}
            width={drawerWidth}
            minWidth={drawerSizing.minWidth}
            maxWidth={drawerSizing.maxWidth}
            keyboardStep={drawerSizing.keyboardStep}
            loading={polymerization.runLoading}
            error={polymerization.runError}
            data={polymerization.data}
            snapshot={snapshot}
            stale={stale}
            onWidthChange={setDrawerWidth}
            onClose={() => setDrawerOpen(false)}
            onOpen={() => setDrawerOpen(true)}
            onClear={clearResults}
          /> : null}
        </div>
      </div>
    </div>
  );
  return showModeSwitcher ? (
    <BatchPolymerizationPanel status={polymerization.status} target={form.targetClass}>
      {renderWorkbench}
    </BatchPolymerizationPanel>
  ) : renderWorkbench();
}
