import {
  Braces,
  CheckCircle2,
  CircleOff,
  FileJson,
  Plus,
  RotateCcw,
  Save,
  Trash2,
  TriangleAlert
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type {
  MonomerMdFormalProtocol,
  MonomerMdProtocolCatalogResponse
} from "../../types";
import {
  buildConfigFromStructuredDraft,
  configFingerprint,
  configsEqual,
  estimatedFormalSteps,
  FORMAL_PROTOCOLS,
  parseAndValidateFormalConfig,
  structuredDraftFromConfig,
  type StructuredFormalDraft,
  validateFormalConfig
} from "./config";
import { formatNumber } from "./presentation";

type MonomerMdFormalConfigProps = {
  protocol: MonomerMdFormalProtocol;
  config: Record<string, unknown> | null;
  catalog: MonomerMdProtocolCatalogResponse | null;
  canSubmit: boolean;
  submissionReason: string;
  isSubmitting: boolean;
  templateChangeCount: number;
  onProtocolChange: (protocol: MonomerMdFormalProtocol) => void;
  onApplyConfig: (protocol: MonomerMdFormalProtocol, config: Record<string, unknown>) => void;
  onRestoreTemplate: (protocol: MonomerMdFormalProtocol) => void;
  onKeepChangedTemplates: () => void;
  onRestoreChangedTemplates: () => void;
  onSubmit: () => void;
};

function updateRow(
  draft: StructuredFormalDraft,
  id: string,
  patch: Partial<StructuredFormalDraft["components"][number]>
): StructuredFormalDraft {
  return {
    ...draft,
    components: draft.components.map((row) => row.id === id ? { ...row, ...patch } : row)
  };
}

export function MonomerMdFormalConfig({
  protocol,
  config,
  catalog,
  canSubmit,
  submissionReason,
  isSubmitting,
  templateChangeCount,
  onProtocolChange,
  onApplyConfig,
  onRestoreTemplate,
  onKeepChangedTemplates,
  onRestoreChangedTemplates,
  onSubmit
}: MonomerMdFormalConfigProps) {
  const [editorMode, setEditorMode] = useState<"form" | "json">("form");
  const [structured, setStructured] = useState<StructuredFormalDraft>(() =>
    structuredDraftFromConfig(config ?? {})
  );
  const [formDirty, setFormDirty] = useState(false);
  const [formErrors, setFormErrors] = useState<string[]>([]);
  const [jsonText, setJsonText] = useState(() => JSON.stringify(config ?? {}, null, 2));
  const [jsonDirty, setJsonDirty] = useState(false);
  const [jsonErrors, setJsonErrors] = useState<string[]>([]);

  const fingerprint = config ? configFingerprint(config) : "";
  useEffect(() => {
    setStructured(structuredDraftFromConfig(config ?? {}));
    setFormDirty(false);
    setFormErrors([]);
    setJsonText(JSON.stringify(config ?? {}, null, 2));
    setJsonDirty(false);
    setJsonErrors([]);
  }, [fingerprint, protocol]);

  const protocolInfo = catalog?.protocols.find((item) => item.protocol === protocol);
  const protocolReady = Boolean(
    catalog?.available &&
    protocolInfo &&
    protocolInfo.supported !== false &&
    protocolInfo.runtime_ready === true
  );
  const configValidation = config ? validateFormalConfig(config, protocol) : { valid: false, errors: ["等待服务协议模板。"] };
  const steps = config ? estimatedFormalSteps(protocol, config) : null;
  const hasUnappliedChanges = formDirty || jsonDirty;
  const submitDisabled =
    isSubmitting ||
    !canSubmit ||
    !protocolReady ||
    !configValidation.valid ||
    hasUnappliedChanges;

  function confirmDiscardUnapplied() {
    if (!hasUnappliedChanges) return true;
    return window.confirm("当前编辑器有未应用的修改。丢弃这些修改并继续吗？");
  }

  function selectProtocol(next: MonomerMdFormalProtocol) {
    if (next === protocol || !confirmDiscardUnapplied()) return;
    onProtocolChange(next);
  }

  function switchEditor(next: "form" | "json") {
    if (next === editorMode) return;
    if (editorMode === "json" && jsonDirty) {
      const discard = window.confirm("高级 JSON 有未应用的修改。确定丢弃并切回结构化表单吗？");
      if (!discard) return;
      setJsonText(JSON.stringify(config ?? {}, null, 2));
      setJsonDirty(false);
      setJsonErrors([]);
    }
    if (editorMode === "form" && formDirty) {
      const discard = window.confirm("结构化表单有未应用的修改。确定丢弃并切换编辑器吗？");
      if (!discard) return;
      setStructured(structuredDraftFromConfig(config ?? {}));
      setFormDirty(false);
      setFormErrors([]);
    }
    setEditorMode(next);
  }

  function changeStructured(patch: Partial<StructuredFormalDraft>) {
    setStructured((current) => ({ ...current, ...patch }));
    setFormDirty(true);
    setFormErrors([]);
  }

  function applyStructured() {
    if (!config) return;
    const result = buildConfigFromStructuredDraft(config, protocol, structured);
    if (!result.config) {
      setFormErrors(result.errors);
      return;
    }
    onApplyConfig(protocol, result.config);
    setFormDirty(false);
    setFormErrors([]);
  }

  function applyJson() {
    const parsed = parseAndValidateFormalConfig(jsonText, protocol);
    if (!parsed.config) {
      setJsonErrors(parsed.errors);
      return;
    }
    onApplyConfig(protocol, parsed.config);
    setJsonDirty(false);
    setJsonErrors([]);
  }

  const protocolState = useMemo(() => Object.fromEntries(
    FORMAL_PROTOCOLS.map((item) => {
      const info = catalog?.protocols.find((candidate) => candidate.protocol === item);
      const ready = Boolean(
        catalog?.available && info && info.supported !== false && info.runtime_ready === true
      );
      return [item, { info, ready }];
    })
  ) as Record<MonomerMdFormalProtocol, {
    info: MonomerMdProtocolCatalogResponse["protocols"][number] | undefined;
    ready: boolean;
  }>, [catalog]);

  return (
    <div className="np-mmd-formal-config">
      {templateChangeCount > 0 ? (
        <div className="np-mmd-template-change" role="status">
          <TriangleAlert />
          <div>
            <strong>检测到 {templateChangeCount} 个服务模板已更新</strong>
            <span>当前草稿已保留，可继续使用或恢复新版默认配置。</span>
          </div>
          <button type="button" onClick={onKeepChangedTemplates}>保留当前草稿</button>
          <button type="button" onClick={onRestoreChangedTemplates}>使用新版配置</button>
        </div>
      ) : null}

      <section className="np-mmd-protocol-section" aria-labelledby="monomer-md-protocol-title">
        <div className="np-mmd-section-heading">
          <div>
            <span className="np-mmd-eyebrow">FULL MD SIMULATION</span>
            <h3 id="monomer-md-protocol-title">选择完整 MD 模拟类型</h3>
          </div>
          <span className="np-mmd-section-note">仅可提交当前已就绪的模拟类型</span>
        </div>
        <div className="np-mmd-protocol-grid">
          {FORMAL_PROTOCOLS.map((item) => {
            const state = protocolState[item];
            return (
              <button
                key={item}
                type="button"
                className={`np-mmd-protocol-card${item === protocol ? " is-selected" : ""}${state.ready ? " is-ready" : " is-unavailable"}`}
                aria-pressed={item === protocol}
                onClick={() => selectProtocol(item)}
              >
                <strong>{item}</strong>
                <span className="np-mmd-protocol-card__status">
                  {state.ready ? <CheckCircle2 /> : <CircleOff />}
                  {state.ready ? "可用" : state.info ? "暂不可用" : "状态确认中"}
                </span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="np-mmd-config-editor" aria-labelledby="monomer-md-config-title">
        <div className="np-mmd-section-heading np-mmd-config-editor__heading">
          <div>
            <span className="np-mmd-eyebrow">CONFIGURATION</span>
            <h3 id="monomer-md-config-title">{protocol} 配置</h3>
          </div>
          <div className="np-mmd-editor-actions">
            <div className="np-mmd-segmented" aria-label="配置编辑方式">
              <button type="button" className={editorMode === "form" ? "is-active" : ""} onClick={() => switchEditor("form")}>
                <Braces />结构化表单
              </button>
              <button type="button" className={editorMode === "json" ? "is-active" : ""} onClick={() => switchEditor("json")}>
                <FileJson />高级 JSON
              </button>
            </div>
            <button
              type="button"
              className="np-mmd-secondary-action"
              onClick={() => {
                if (!config || configsEqual(config, protocolInfo?.default_config ?? {})) {
                  onRestoreTemplate(protocol);
                  return;
                }
                if (window.confirm("恢复默认配置会覆盖当前模拟类型草稿。确定继续吗？")) {
                  onRestoreTemplate(protocol);
                }
              }}
              disabled={!protocolInfo?.default_config}
            >
              <RotateCcw />恢复默认配置
            </button>
          </div>
        </div>

        {!config ? (
          <div className="np-mmd-empty-state">正在加载 {protocol} 默认配置…</div>
        ) : editorMode === "form" ? (
          <div className="np-mmd-structured-editor">
            <div className="np-mmd-fields-grid">
              <label>
                <span>温度 <small>K</small></span>
                <input
                  type="number"
                  min="0"
                  step="any"
                  value={structured.temperature}
                  onChange={(event) => changeStructured({ temperature: event.target.value })}
                />
              </label>
              <label>
                <span>目标原子数</span>
                <input
                  type="number"
                  min="1"
                  max="10000"
                  step="1"
                  value={structured.natoms}
                  onChange={(event) => changeStructured({ natoms: event.target.value })}
                />
              </label>
              {protocol === "Dielectric" || protocol === "Compressibility" ? (
                <label>
                  <span>NPT 步数 <small>{protocol === "Dielectric" ? "留空：2,000,000" : "留空：5,000,000"}</small></span>
                  <input
                    type="number"
                    min={protocol === "Compressibility" ? "1000001" : "1"}
                    step="1"
                    value={structured.nptSteps}
                    placeholder={protocol === "Dielectric" ? "2000000（隐式）" : "5000000（隐式）"}
                    onChange={(event) => changeStructured({ nptSteps: event.target.value })}
                  />
                </label>
              ) : null}
              {protocol === "Dielectric" ? (
                <>
                  <label>
                    <span>NVT 步数 <small>留空：6,000,000</small></span>
                    <input
                      type="number"
                      min="1"
                      step="1"
                      value={structured.nvtSteps}
                      placeholder="6000000（隐式）"
                      onChange={(event) => changeStructured({ nvtSteps: event.target.value })}
                    />
                  </label>
                  <label>
                    <span>偶极采样间隔 <small>留空：500</small></span>
                    <input
                      type="number"
                      min="1"
                      step="1"
                      value={structured.dipoleInterval}
                      placeholder="500（隐式）"
                      onChange={(event) => changeStructured({ dipoleInterval: event.target.value })}
                    />
                  </label>
                </>
              ) : null}
            </div>

            <div className="np-mmd-components-heading">
              <div>
                <strong>体系组分</strong>
                <span>每个组分名称需保持唯一，并填写对应配比和结构</span>
              </div>
              <button
                type="button"
                disabled={protocol === "HVap" && structured.components.length >= 1}
                onClick={() => {
                  changeStructured({
                    components: [
                      ...structured.components,
                      { id: `component-new-${Date.now()}`, name: "", ratio: "1", smiles: "" }
                    ]
                  });
                }}
              >
                <Plus />添加组分
              </button>
            </div>
            <div className="np-mmd-components-table" role="group" aria-label="完整 MD 模拟体系组分">
              <div className="np-mmd-components-table__head" aria-hidden="true">
                <span>名称</span><span>摩尔配比</span><span>SMILES</span><span>操作</span>
              </div>
              {structured.components.map((row, index) => (
                <div className="np-mmd-component-row" key={row.id}>
                  <label><span className="np-mmd-mobile-label">名称</span><input aria-label={`组分 ${index + 1} 名称`} value={row.name} onChange={(event) => { setStructured((current) => updateRow(current, row.id, { name: event.target.value })); setFormDirty(true); setFormErrors([]); }} /></label>
                  <label><span className="np-mmd-mobile-label">摩尔配比</span><input aria-label={`组分 ${index + 1} 摩尔配比`} type="number" min="0" step="any" value={row.ratio} onChange={(event) => { setStructured((current) => updateRow(current, row.id, { ratio: event.target.value })); setFormDirty(true); setFormErrors([]); }} /></label>
                  <label><span className="np-mmd-mobile-label">SMILES</span><input aria-label={`组分 ${index + 1} SMILES`} className="np-mmd-mono-input" value={row.smiles} spellCheck={false} onChange={(event) => { setStructured((current) => updateRow(current, row.id, { smiles: event.target.value })); setFormDirty(true); setFormErrors([]); }} /></label>
                  <button
                    type="button"
                    aria-label={`删除组分 ${index + 1}`}
                    disabled={structured.components.length <= 1}
                    onClick={() => changeStructured({ components: structured.components.filter((item) => item.id !== row.id) })}
                  ><Trash2 /></button>
                </div>
              ))}
            </div>

            {formErrors.length ? <ErrorList id="monomer-md-form-errors" errors={formErrors} /> : null}
            <div className="np-mmd-apply-row">
              <span>{formDirty ? "表单修改尚未应用，应用后才能提交。" : "当前表单与合法配置一致。"}</span>
              <button type="button" onClick={applyStructured} disabled={!formDirty}>
                <Save />应用表单修改
              </button>
            </div>
          </div>
        ) : (
          <div className="np-mmd-json-editor">
            <p>
              未知顶层字段、嵌套对象和数组会原样保留。输入期间允许暂时无效，只有点击“应用 JSON”才会更新合法配置。
            </p>
            <textarea
              aria-label="完整 MD 模拟高级 JSON"
              value={jsonText}
              spellCheck={false}
              aria-invalid={jsonErrors.length ? true : undefined}
              aria-describedby={jsonErrors.length ? "monomer-md-json-errors" : "monomer-md-json-hint"}
              onChange={(event) => {
                setJsonText(event.target.value);
                setJsonDirty(true);
                setJsonErrors([]);
              }}
            />
            <span id="monomer-md-json-hint" className="np-mmd-field-hint">
              protocol 必须为 {protocol}。
            </span>
            {jsonErrors.length ? <ErrorList id="monomer-md-json-errors" errors={jsonErrors} /> : null}
            <div className="np-mmd-apply-row">
              <span>{jsonDirty ? "JSON 修改尚未应用。" : "当前 JSON 已应用。"}</span>
              <button type="button" onClick={applyJson} disabled={!jsonDirty}>
                <Save />应用 JSON
              </button>
            </div>
          </div>
        )}
      </section>

      <div className="np-mmd-submit-bar">
        <div>
          <span>预计模拟步数</span>
          <strong>{steps == null ? "--" : formatNumber(steps, 0)}</strong>
          {protocol === "Dielectric" ? (
            <small>字段缺失时按隐式 2M NPT + 6M NVT 计算；服务模板显式为 2M + 8M。</small>
          ) : null}
        </div>
        <div className="np-mmd-submit-bar__action">
          <span>{hasUnappliedChanges ? "请先应用编辑器修改" : protocolReady ? submissionReason : "当前协议尚未确认运行时就绪"}</span>
          <button type="button" disabled={submitDisabled} onClick={onSubmit}>
            {isSubmitting ? "正在创建任务" : "提交完整 MD 模拟任务"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ErrorList({ id, errors }: { id: string; errors: string[] }) {
  return (
    <div id={id} className="np-mmd-error-list" role="alert">
      <TriangleAlert />
      <ul>{errors.map((error) => <li key={error}>{error}</li>)}</ul>
    </div>
  );
}
