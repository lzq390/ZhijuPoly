import {
  CheckCircle2,
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
      ? "可用"
      : serviceState === "error"
        ? "检查失败"
        : "不可用";
  const drawerSizing = native2K
    ? { minWidth: 480, maxWidth: 720, keyboardStep: 24 }
    : { minWidth: 320, maxWidth: 560, keyboardStep: 16 };
  const workbenchStyle = { "--np-sw-drawer-width": `${drawerWidth}px` } as CSSProperties;

  return (
    <div
      className="np-structure-workbench np-monomer-polymerization"
      data-module="monomer-polymerization"
      style={workbenchStyle}
    >
      <div className={`np-sw-page np-mp-page${drawerOpen ? " has-open-drawer" : ""}`}>
        <h1 className="np-sw-page-title">单体正向聚合</h1>
        <div className={`np-sw-layout${drawerOpen ? " has-open-drawer" : ""}`}>
          <main className="np-sw-workspace">
            <div className="np-mp-scroll-region">
              <form
                className="np-mp-surface np-sw-accented-surface"
                onSubmit={submitPolymerization}
                noValidate
              >
                <header className="np-mp-surface__header">
                  <div>
                    <span>SMIPOLY FORWARD POLYMERIZATION</span>
                    <h2>由普通单体生成规则候选</h2>
                    <p>设置目标类别与一至两个单体，候选将在右侧结果抽屉中返回。</p>
                  </div>
                  <div className="np-mp-service-status">
                    <span className={`is-${serviceState}`} role="status">
                      {serviceState === "loading" ? <LoaderCircle className="np-sw-spin" /> : null}
                      {serviceState === "ready" ? <CheckCircle2 /> : null}
                      {serviceState === "unavailable" ? <CircleOff /> : null}
                      {serviceState === "error" ? <TriangleAlert /> : null}
                      {serviceLabel}
                    </span>
                    <button
                      type="button"
                      aria-label="刷新 SMiPoly 服务状态"
                      onClick={() => void polymerization.refreshStatus()}
                      disabled={polymerization.statusLoading}
                    >
                      <RefreshCw aria-hidden="true" />
                      刷新
                    </button>
                  </div>
                </header>

                {polymerization.statusError || serviceState === "unavailable" ? (
                  <div className="np-mp-service-message" role="alert">
                    <TriangleAlert aria-hidden="true" />
                    <span>
                      {polymerization.statusError ?? polymerization.status?.message ?? "SMiPoly 服务当前不可用。"}
                    </span>
                  </div>
                ) : null}

                <section className="np-mp-section" aria-labelledby="np-mp-target-title">
                  <header className="np-mp-section__header">
                    <span>01</span>
                    <div>
                      <h2 id="np-mp-target-title">目标聚合物类型</h2>
                      <p>服务返回的类别与单体数量要求优先于本地兼容规则。</p>
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
                          <span>{targetRequirement.monomer_b_required ? "双单体规则" : "单/双单体规则"}</span>
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
                      <p>A/B 为平级输入槽，可分别导入同一全局共享结构并生成独立 2D 预览。</p>
                    </div>
                  </header>
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
                </section>

                <section className="np-mp-section" aria-labelledby="np-mp-settings-title">
                  <header className="np-mp-section__header">
                    <span>03</span>
                    <div>
                      <h2 id="np-mp-settings-title">运行设置</h2>
                      <p>同步调用 SMiPoly；总命中数可能大于本次实际返回数。</p>
                    </div>
                  </header>
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
                      <small id="np-mp-max-results-hint">服务上限：{maxResultsLimit}</small>
                      {maxResultsError ? (
                        <small id="np-mp-max-results-error" className="np-mp-field-error" role="alert">
                          {maxResultsError}
                        </small>
                      ) : null}
                    </label>
                    <div className="np-mp-run-note">
                      <FlaskConical aria-hidden="true" />
                      <p>规则候选不代表真实可合成性或性质验证；结果需要结合实验条件进一步评估。</p>
                    </div>
                  </div>

                  <div className="np-mp-form-actions">
                    <button type="submit" className="np-sw-primary-button" disabled={!canSubmit}>
                      {polymerization.runLoading ? <LoaderCircle className="np-sw-spin" /> : <Play />}
                      {polymerization.runLoading ? "运行中" : "运行聚合"}
                    </button>
                    <button type="button" className="np-sw-secondary-button" onClick={resetForm}>
                      <RotateCcw aria-hidden="true" />
                      重置表单
                    </button>
                  </div>
                </section>
              </form>
            </div>
          </main>

          <MonomerPolymerizationDrawer
            open={drawerOpen}
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
          />
        </div>
      </div>
    </div>
  );
}
