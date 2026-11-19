import { Check, ChevronDown, Download, LoaderCircle, Play, TriangleAlert } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent, type RefObject } from "react";
import {
  PREDICTABLE_PROPERTIES,
  PREDICT_PROPERTY_CATALOG,
  PREDICT_PROPERTY_GROUPS,
  PREDICT_PROPERTY_META,
  type PredictPropertyGroup
} from "../../constants/predictableProperties";
import type { PredictableProperty } from "../../types";
import {
  displaySmiles,
  formatPredictionValue,
  formatRunTime,
  predictionDelta,
  predictionTable,
  predictionValue,
  toCsv,
  toTsv,
  type PredictionRun
} from "./predictionFormat";

export type PredictionRelayModule = "databaseQuery" | "explorer" | "mdSimulationDemo";

const RELAY_LINKS: readonly { id: PredictionRelayModule; label: string }[] = [
  { id: "databaseQuery", label: "数据库查询" },
  { id: "explorer", label: "相似性探索" },
  { id: "mdSimulationDemo", label: "MD 模拟" }
];

const SHORT_NAMES: Record<PredictableProperty, string> = {
  "Glass transition temperature": "Tg",
  "Melting temperature": "Tm",
  "Thermal decomposition temperature": "Td",
  "Thermal decomposition weight loss": "失重率",
  "Elongation at break": "断裂伸长率",
  "Tensile stress strength at break": "拉伸强度",
  "O2 Permeability Barrer": "O₂",
  "Co2 Permeability Barrer": "CO₂",
  "H2 Permeability Barrer": "H₂"
};

const GROUP_LABELS: Record<PredictPropertyGroup, string> = { thermal: "热学", mechanical: "力学", permeability: "气体渗透" };

type HomopolymerPredictionPanelProps = {
  formRef: RefObject<HTMLFormElement | null>;
  selectedProperties: readonly PredictableProperty[];
  onSelectedPropertiesChange: (properties: PredictableProperty[]) => void;
  selectionOpen: boolean;
  onSelectionOpenChange: (open: boolean) => void;
  loading: boolean;
  error: string | null;
  hasAttempt: boolean;
  structureMissing: boolean;
  latestRun: PredictionRun | null;
  runCount: number;
  stale: boolean;
  comparison: PredictionRun | null;
  comparisonOptions: readonly PredictionRun[];
  onComparisonChange: (runId: number | null) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onRetry: () => void;
  onOpenModule?: (module: PredictionRelayModule) => void;
};

function selectionSummary(selected: readonly PredictableProperty[]) {
  if (selected.length === 0) return { title: "未选择性质", detail: "至少选择一项性质后才能运行预测" };
  if (selected.length === PREDICTABLE_PROPERTIES.length) {
    const detail = PREDICT_PROPERTY_GROUPS.map((group) => {
      const count = PREDICT_PROPERTY_CATALOG.filter((property) => property.group === group.key).length;
      return `${GROUP_LABELS[group.key]} ${count} 项`;
    }).join("，");
    return { title: `全部 ${selected.length} 项性质`, detail };
  }
  return {
    title: `已选 ${selected.length} / ${PREDICTABLE_PROPERTIES.length} 项`,
    detail: PREDICTABLE_PROPERTIES.filter((property) => selected.includes(property)).map((property) => SHORT_NAMES[property]).join("、")
  };
}

function groupedProperties(properties: readonly PredictableProperty[]) {
  return PREDICT_PROPERTY_GROUPS.map((group) => ({
    group,
    properties: properties.filter((property) => PREDICT_PROPERTY_META[property].group === group.key)
  })).filter((entry) => entry.properties.length > 0);
}

function download(filename: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function ExportMenu({ run, comparison }: { run: PredictionRun; comparison: PredictionRun | null }) {
  const [open, setOpen] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    rootRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    function handlePointer(event: PointerEvent) {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) setOpen(false);
    }
    function handleKey(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      buttonRef.current?.focus();
    }
    document.addEventListener("pointerdown", handlePointer);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("pointerdown", handlePointer);
      document.removeEventListener("keydown", handleKey);
    };
  }, [open]);

  useEffect(() => {
    if (!feedback) return;
    const timer = window.setTimeout(() => setFeedback(null), 2400);
    return () => window.clearTimeout(timer);
  }, [feedback]);

  const table = () => predictionTable(run, comparison);

  return (
    <div ref={rootRef} className="np-hp-export">
      {feedback ? <span className="np-hp-export__feedback" role="status">{feedback}</span> : null}
      <button
        ref={buttonRef}
        type="button"
        className="np-hp-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        onClick={() => setOpen((current) => !current)}
      >
        <Download aria-hidden="true" />
        <span>导出</span>
        <ChevronDown aria-hidden="true" />
      </button>
      {open ? (
        <div id={menuId} className="np-hp-export__menu" role="menu" aria-label="导出预测结果">
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              download(`homopolymer-prediction-${run.id}.csv`, toCsv(table()));
              setOpen(false);
              setFeedback("已下载 CSV");
            }}
          >
            下载 CSV
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={async () => {
              setOpen(false);
              try {
                await navigator.clipboard.writeText(toTsv(table()));
                setFeedback("已复制，可粘贴到表格");
              } catch {
                setFeedback("复制失败，请改用下载 CSV");
              }
            }}
          >
            复制表格
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ResultTable({
  properties,
  run,
  comparison,
  placeholder = false
}: {
  properties: readonly PredictableProperty[];
  run: PredictionRun | null;
  comparison: PredictionRun | null;
  placeholder?: boolean;
}) {
  const compare = Boolean(comparison) && !placeholder;
  return (
    <table className={`np-hp-table${placeholder ? " is-placeholder" : ""}`}>
      <thead>
        <tr>
          <th scope="col">性质</th>
          <th scope="col" className="is-num">本次</th>
          {compare ? <th scope="col" className="is-num">上次</th> : null}
          {compare ? <th scope="col" className="is-num">变化</th> : null}
        </tr>
      </thead>
      {groupedProperties(properties).map(({ group, properties: groupProperties }) => (
        <tbody key={group.key} className={`is-${group.key}`}>
          <tr className="np-hp-table__group">
            <th scope="rowgroup" colSpan={compare ? 4 : 2}>
              <i aria-hidden="true" />
              {GROUP_LABELS[group.key]}
            </th>
          </tr>
          {groupProperties.map((property) => {
            const meta = PREDICT_PROPERTY_META[property];
            const value = placeholder ? null : predictionValue(run, property);
            const previous = compare ? predictionValue(comparison, property) : null;
            const delta = compare ? predictionDelta(value, previous) : null;
            const formatted = formatPredictionValue(value);
            const previousFormatted = formatPredictionValue(previous);
            return (
              <tr key={property}>
                <th scope="row">
                  <span>{meta.label}</span>
                  {meta.shortLabel.length <= 3 ? <small>{meta.shortLabel}</small> : null}
                </th>
                <td className="is-num" title={value !== null ? `${value} ${meta.unit}` : undefined}>
                  {placeholder ? (
                    <span className="np-hp-table__placeholder">—</span>
                  ) : formatted !== null ? (
                    <><strong>{formatted}</strong><span className="np-hp-table__unit">{meta.unit}</span></>
                  ) : (
                    <span className="np-hp-table__missing">未返回</span>
                  )}
                </td>
                {compare ? (
                  <td className="is-num is-previous" title={previous !== null ? `${previous} ${meta.unit}` : undefined}>
                    {previousFormatted ?? <span className="np-hp-table__missing">—</span>}
                  </td>
                ) : null}
                {compare ? (
                  <td className={`is-num is-delta${delta?.large ? " is-large" : ""}`}>
                    {delta?.text ?? <span className="np-hp-table__missing">—</span>}
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      ))}
    </table>
  );
}

export function HomopolymerPredictionPanel({
  formRef,
  selectedProperties,
  onSelectedPropertiesChange,
  selectionOpen,
  onSelectionOpenChange,
  loading,
  error,
  hasAttempt,
  structureMissing,
  latestRun,
  runCount,
  stale,
  comparison,
  comparisonOptions,
  onComparisonChange,
  onSubmit,
  onRetry,
  onOpenModule
}: HomopolymerPredictionPanelProps) {
  const selectionId = useId();
  const resultsTitleId = useId();
  const selected = new Set(selectedProperties);
  const summary = selectionSummary(selectedProperties);

  function toggleProperty(property: PredictableProperty) {
    onSelectedPropertiesChange(
      PREDICTABLE_PROPERTIES.filter((item) => (item === property ? !selected.has(item) : selected.has(item)))
    );
  }

  function toggleGroup(group: PredictPropertyGroup) {
    const groupProperties = PREDICT_PROPERTY_CATALOG.filter((property) => property.group === group).map((property) => property.key);
    const allSelected = groupProperties.every((property) => selected.has(property));
    onSelectedPropertiesChange(
      PREDICTABLE_PROPERTIES.filter((property) => (groupProperties.includes(property) ? !allSelected : selected.has(property)))
    );
  }

  const tableProperties = latestRun?.properties ?? PREDICTABLE_PROPERTIES.filter((property) => selected.has(property));

  return (
    <aside className="np-hp-panel" aria-label="性质预测">
      <form ref={formRef} className="np-hp-task" noValidate onSubmit={onSubmit}>
        <section className="np-hp-selection" aria-labelledby={`${selectionId}-label`}>
          <h2 id={`${selectionId}-label`} className="np-hp-label">预测内容</h2>
          <div className="np-hp-selection__summary">
            <div>
              <strong>{summary.title}</strong>
              <small>{summary.detail}</small>
            </div>
            <button
              type="button"
              className="np-hp-link"
              aria-expanded={selectionOpen}
              aria-controls={`${selectionId}-editor`}
              onClick={() => onSelectionOpenChange(!selectionOpen)}
            >
              {selectionOpen ? "完成" : "调整"}
            </button>
          </div>
          {selectionOpen ? (
            <div id={`${selectionId}-editor`} className="np-hp-selection__editor">
              {PREDICT_PROPERTY_GROUPS.map((group) => {
                const properties = PREDICT_PROPERTY_CATALOG.filter((property) => property.group === group.key);
                const groupSelected = properties.filter((property) => selected.has(property.key)).length;
                return (
                  <fieldset key={group.key} className={`np-hp-group is-${group.key}`}>
                    <legend>
                      <i aria-hidden="true" />
                      {GROUP_LABELS[group.key]}
                      <button type="button" className="np-hp-link" onClick={() => toggleGroup(group.key)}>
                        {groupSelected === properties.length ? "取消本组" : "选择本组"}
                      </button>
                    </legend>
                    <div className="np-hp-group__options">
                      {properties.map((property) => {
                        const checked = selected.has(property.key);
                        return (
                          <label key={property.key} className={`np-hp-chip${checked ? " is-selected" : ""}`}>
                            <input type="checkbox" checked={checked} onChange={() => toggleProperty(property.key)} />
                            <span className="np-hp-chip__check" aria-hidden="true">{checked ? <Check /> : null}</span>
                            <span>{property.label}</span>
                          </label>
                        );
                      })}
                    </div>
                  </fieldset>
                );
              })}
              <div className="np-hp-selection__bulk">
                <button
                  type="button"
                  className="np-hp-link"
                  onClick={() => onSelectedPropertiesChange([...PREDICTABLE_PROPERTIES])}
                  disabled={selectedProperties.length === PREDICTABLE_PROPERTIES.length}
                >
                  全选
                </button>
                <button
                  type="button"
                  className="np-hp-link"
                  onClick={() => onSelectedPropertiesChange([])}
                  disabled={selectedProperties.length === 0}
                >
                  清空
                </button>
              </div>
            </div>
          ) : null}
        </section>

        <div className="np-hp-run">
          <button
            type="submit"
            className="np-hp-primary"
            aria-busy={loading}
            disabled={selectedProperties.length === 0}
            title="快捷键 Ctrl + Enter"
            aria-keyshortcuts="Control+Enter Meta+Enter"
          >
            {loading ? <LoaderCircle className="np-sw-spin" aria-hidden="true" /> : <Play aria-hidden="true" />}
            {loading ? "重新预测" : "运行预测"}
          </button>
          {structureMissing ? (
            <p className="np-hp-hint" role="alert">请先在左侧画出结构，或在 SMILES 框中输入结构。</p>
          ) : null}
        </div>
      </form>

      <section className="np-hp-results" aria-labelledby={resultsTitleId} aria-busy={loading}>
        <header className="np-hp-results__header">
          <h2 id={resultsTitleId}>
            预测结果
            {latestRun && !loading ? (
              <small>
                {runCount > 1 ? `第 ${runCount} 次运行，` : ""}
                {latestRun.queryTimeMs.toFixed(1)} ms
              </small>
            ) : null}
          </h2>
          {latestRun && !loading && !error ? <ExportMenu run={latestRun} comparison={comparison} /> : null}
        </header>

        {loading ? (
          <p className="np-hp-status" role="status">
            <LoaderCircle className="np-sw-spin" aria-hidden="true" />
            正在计算 {selectedProperties.length} 项性质…
          </p>
        ) : null}

        {!loading && error ? (
          <div className="np-hp-alert" role="alert">
            <TriangleAlert aria-hidden="true" />
            <div>
              <strong>性质预测失败</strong>
              <p>{error}</p>
            </div>
            <button type="button" className="np-hp-button" onClick={onRetry}>重试</button>
          </div>
        ) : null}

        {!loading && !error && latestRun ? (
          <>
            {stale ? (
              <>
                <p className="np-hp-notice" role="status">当前结构或性质选择已变化；下列结果对应上次提交参数。</p>
                <p className="np-hp-submitted">
                  <span>提交结构</span>
                  <code title={latestRun.smiles}>{displaySmiles(latestRun.smiles)}</code>
                </p>
              </>
            ) : null}
            {comparisonOptions.length > 0 ? (
              <label className="np-hp-compare">
                <span>对比</span>
                <select
                  value={comparison?.id ?? "none"}
                  onChange={(event) => onComparisonChange(event.currentTarget.value === "none" ? null : Number(event.currentTarget.value))}
                >
                  <option value="none">不对比</option>
                  {comparisonOptions.map((run) => (
                    <option key={run.id} value={run.id}>{`${formatRunTime(run.finishedAt)}  ${displaySmiles(run.smiles)}`}</option>
                  ))}
                </select>
              </label>
            ) : null}
            <div className="np-hp-table-wrap">
              <ResultTable properties={latestRun.properties} run={latestRun} comparison={comparison} />
            </div>
            <p className="np-hp-footnote">
              模型预测值，需结合实验验证{comparison ? "；加粗表示变化超过 20%" : ""}。
            </p>
          </>
        ) : null}

        {!loading && !error && !latestRun ? (
          <>
            <p className="np-hp-empty">
              {hasAttempt ? "上次预测没有返回结果。" : "画好结构后点“运行预测”。结果会在这里按热学、力学、气体渗透分组列出。"}
            </p>
            {tableProperties.length ? (
              <div className="np-hp-table-wrap">
                <ResultTable properties={tableProperties} run={null} comparison={null} placeholder />
              </div>
            ) : null}
          </>
        ) : null}

        {loading && latestRun ? (
          <div className="np-hp-table-wrap is-dimmed" aria-hidden="true">
            <ResultTable properties={latestRun.properties} run={latestRun} comparison={null} />
          </div>
        ) : null}
        {loading && !latestRun && tableProperties.length ? (
          <div className="np-hp-table-wrap" aria-hidden="true">
            <ResultTable properties={tableProperties} run={null} comparison={null} placeholder />
          </div>
        ) : null}
      </section>

      {onOpenModule ? (
        <nav className="np-hp-next" aria-label="用这个结构继续">
          <span>用这个结构继续</span>
          {RELAY_LINKS.map((link) => (
            <button key={link.id} type="button" className="np-hp-link" onClick={() => onOpenModule(link.id)}>
              {link.label}
            </button>
          ))}
        </nav>
      ) : null}
    </aside>
  );
}
