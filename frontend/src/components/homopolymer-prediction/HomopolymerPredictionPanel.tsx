import { Atom, Check, ChevronDown, Database, Download, LoaderCircle, Network, Play, TriangleAlert } from "lucide-react";
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
import { PROPERTY_SCALES, scaleBounds, scalePosition, scaleRangeLabel, tickPercent } from "./predictionScale";
import { ChannelBadge } from "./channelIcons";

export type PredictionRelayModule = "databaseQuery" | "explorer" | "mdSimulationDemo";

const RELAY_LINKS = [
  { id: "databaseQuery", label: "数据库查询", Icon: Database },
  { id: "explorer", label: "相似性探索", Icon: Network },
  { id: "mdSimulationDemo", label: "MD 模拟", Icon: Atom }
] as const satisfies readonly { id: PredictionRelayModule; label: string; Icon: typeof Database }[];

const SHORTCUT_LABEL = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ ↵" : "Ctrl ↵";

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
  /** 1-based position of the displayed run within this session. */
  runNumber: number;
  /** All runs of this session, oldest first. */
  runs: readonly PredictionRun[];
  stale: boolean;
  comparison: PredictionRun | null;
  comparisonOptions: readonly PredictionRun[];
  onComparisonChange: (runId: number | null) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onRetry: () => void;
  onRestoreRun: (run: PredictionRun) => void;
  /** Relay links only make sense once there is a structure to carry over. */
  showRelay: boolean;
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

/** The readout rail: fixed display range, filled dot for this run, ring for the compared run. */
function ReadoutScale({
  property,
  unit,
  value,
  previous
}: {
  property: PredictableProperty;
  unit: string;
  value: number | null;
  previous: number | null;
}) {
  const current = scalePosition(property, value);
  const before = scalePosition(property, previous);
  const [low, high] = scaleBounds(property);
  const span = current && before ? [Math.min(current.percent, before.percent), Math.abs(current.percent - before.percent)] : null;
  return (
    <td className="np-hp-scale" aria-hidden="true">
      <span className="np-hp-scale__rail" title={`刻度 ${scaleRangeLabel(property, unit)}`}>
        {PROPERTY_SCALES[property].ticks.map((tick) => (
          <i key={tick} style={{ left: `${tickPercent(property, tick)}%` }} />
        ))}
        {span ? <span className="np-hp-scale__span" style={{ left: `${span[0]}%`, width: `${span[1]}%` }} /> : null}
        {current ? (
          <b
            className={`np-hp-scale__mark${current.outside ? ` is-${current.outside}` : ""}`}
            style={{ left: `${current.percent}%` }}
          />
        ) : null}
        {/* The ring is drawn last and hollow, so a nearby filled dot stays visible through it. */}
        {before ? <b className="np-hp-scale__mark is-previous" style={{ left: `${before.percent}%` }} /> : null}
      </span>
      {/* Range ends are drawn by CSS from these attributes, so they never read as values. */}
      <span className="np-hp-scale__ends" data-low={low} data-high={high} />
    </td>
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
    <table className={`np-hp-table${placeholder ? " is-placeholder" : ""}${compare ? " is-comparing" : ""}`}>
      <thead>
        <tr>
          <th scope="col">性质</th>
          <th aria-hidden="true" className="np-hp-table__scale-head" />
          <th scope="col" className="is-num">本次</th>
          {compare ? <th scope="col" className="is-num">上次</th> : null}
          {compare ? <th scope="col" className="is-num">变化</th> : null}
        </tr>
      </thead>
      {groupedProperties(properties).map(({ group, properties: groupProperties }) => (
        <tbody key={group.key} className={`is-${group.key}`}>
          <tr className="np-hp-table__group">
            <th scope="rowgroup" colSpan={compare ? 5 : 3}>
              <ChannelBadge group={group.key} />
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
            const direction = value !== null && previous !== null && value !== previous ? (value > previous ? " is-up" : " is-down") : "";
            return (
              <tr key={property}>
                <th scope="row">
                  <span>{meta.label}</span>
                  {meta.shortLabel.length <= 3 ? <small>{meta.shortLabel}</small> : null}
                </th>
                <ReadoutScale property={property} unit={meta.unit} value={value} previous={previous} />
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
                  <td className={`is-num is-delta${delta?.large ? " is-large" : ""}${direction}`}>
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

function SessionRuns({
  runs,
  current,
  comparison,
  onRestore
}: {
  runs: readonly PredictionRun[];
  current: PredictionRun;
  comparison: PredictionRun | null;
  onRestore: (run: PredictionRun) => void;
}) {
  const headingId = useId();
  return (
    <section className="np-hp-runs" aria-labelledby={headingId}>
      <header>
        <h3 id={headingId}>本次会话的运行</h3>
        <small>点一条可恢复结构和结果</small>
      </header>
      <ol>
        {[...runs].reverse().map((run) => {
          const isCurrent = run.id === current.id;
          const tg = formatPredictionValue(predictionValue(run, "Glass transition temperature"));
          const role = isCurrent ? "当前" : comparison?.id === run.id ? "对比中" : null;
          return (
            <li key={run.id}>
              <button
                type="button"
                className={isCurrent ? "is-current" : undefined}
                aria-current={isCurrent ? "true" : undefined}
                disabled={isCurrent}
                onClick={() => onRestore(run)}
              >
                <i
                  className={`np-hp-runs__mark${isCurrent ? " is-current" : comparison?.id === run.id ? " is-previous" : ""}`}
                  aria-hidden="true"
                />
                <span className="np-hp-runs__main">
                  <code title={run.smiles}>{displaySmiles(run.smiles)}</code>
                  <small>{formatRunTime(run.finishedAt)}，{run.properties.length} 项{role ? `，${role}` : ""}</small>
                </span>
                {tg !== null ? <span className="np-hp-runs__tg">Tg <strong>{tg}</strong> °C</span> : null}
              </button>
            </li>
          );
        })}
      </ol>
    </section>
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
  runNumber,
  runs,
  stale,
  comparison,
  comparisonOptions,
  onComparisonChange,
  onSubmit,
  onRetry,
  onRestoreRun,
  showRelay,
  onOpenModule
}: HomopolymerPredictionPanelProps) {
  const selectionId = useId();
  const resultsTitleId = useId();
  const [resultsScrolled, setResultsScrolled] = useState(false);
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
    <aside className={`np-hp-panel${resultsScrolled ? " is-results-scrolled" : ""}`} aria-label="性质预测">
      <form ref={formRef} id="np-hp-task" className="np-hp-task" noValidate onSubmit={onSubmit}>
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
              {selectionOpen ? "收起" : "调整"}
            </button>
          </div>
          {!selectionOpen ? (
            <ol className="np-hp-meter" aria-hidden="true">
              {PREDICT_PROPERTY_CATALOG.map((property) => (
                <li key={property.key} className={`is-${property.group}${selected.has(property.key) ? " is-on" : ""}`} />
              ))}
            </ol>
          ) : null}
          {selectionOpen ? (
            <div id={`${selectionId}-editor`} className="np-hp-selection__editor">
              {PREDICT_PROPERTY_GROUPS.map((group) => {
                const properties = PREDICT_PROPERTY_CATALOG.filter((property) => property.group === group.key);
                const groupSelected = properties.filter((property) => selected.has(property.key)).length;
                return (
                  <fieldset key={group.key} className={`np-hp-group is-${group.key}`}>
                    <legend>
                      <ChannelBadge group={group.key} />
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
            <span>{loading ? "重新预测" : "运行预测"}</span>
            <kbd className="np-hp-primary__keys" aria-hidden="true">{SHORTCUT_LABEL}</kbd>
          </button>
          {structureMissing ? (
            <p className="np-hp-hint" role="alert">请先在左侧画出结构，或在 SMILES 框中输入结构。</p>
          ) : null}
        </div>
      </form>

      <section
        className="np-hp-results"
        aria-labelledby={resultsTitleId}
        aria-busy={loading}
        onScroll={(event) => setResultsScrolled(event.currentTarget.scrollTop > 2)}
      >
        <header className="np-hp-results__header">
          <h2 id={resultsTitleId}>
            预测结果
            {latestRun && !loading ? (
              <small className="np-hp-results__meta">
                {runs.length > 1 ? <span className="np-hp-badge">第 {runNumber} 次运行</span> : null}
                <span>{latestRun.queryTimeMs.toFixed(1)} ms</span>
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
              {/[\u4e00-\u9fff]/.test(error) ? (
                <p>{error}</p>
              ) : (
                <>
                  <p>预测服务没有返回结果。可以稍后重试；如果刚修改过结构，先确认 SMILES 有效。</p>
                  <p className="np-hp-alert__detail">服务信息：<code>{error}</code></p>
                </>
              )}
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
              <div className="np-hp-compare-row">
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
                {comparison ? (
                  <span className="np-hp-legend" aria-hidden="true">
                    <span><i className="is-current" />本次</span>
                    <span><i className="is-previous" />对比</span>
                  </span>
                ) : null}
              </div>
            ) : null}
            <div className="np-hp-table-wrap">
              <ResultTable properties={latestRun.properties} run={latestRun} comparison={comparison} />
            </div>
            <p className="np-hp-footnote">
              模型预测值，需结合实验验证{comparison ? "；加粗表示变化超过 20%" : ""}。
            </p>
            {runs.length > 1 ? (
              <SessionRuns runs={runs} current={latestRun} comparison={comparison} onRestore={onRestoreRun} />
            ) : null}
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

      {onOpenModule && showRelay ? (
        <nav className="np-hp-next" aria-label="用这个结构继续">
          <span>用这个结构继续</span>
          {RELAY_LINKS.map(({ id, label, Icon }) => (
            <button key={id} type="button" className="np-hp-relay" onClick={() => onOpenModule(id)}>
              <Icon aria-hidden="true" />
              {label}
            </button>
          ))}
        </nav>
      ) : null}
    </aside>
  );
}
