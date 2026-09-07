import {
  ArrowDownRight, ArrowLeft, ArrowUpRight, BadgeInfo, Bot, CheckCircle2, ChevronDown, ChevronLeft,
  ChevronRight, Circle, Download, FileSpreadsheet, Layers3, LoaderCircle,
  UploadCloud, TriangleAlert,
} from "lucide-react";
import { type CSSProperties, type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";
import {
  highThroughputDemoScenario,
  type HighThroughputDoeCsvFile,
  type HighThroughputTarget,
  type HighThroughputTargetKey,
} from "../../constants/highThroughputDemoScenario";
import { cn } from "../../lib/utils";
import type { PriorDataUploadsState, PriorDataUploadState } from "./types";
import "./prior-import-workspace.css";

type PriorImportWorkspaceProps = {
  targets: HighThroughputTarget[];
  candidateTotal: number;
  materialType: string;
  representation: string;
  activeTargetKey: HighThroughputTargetKey;
  onSelectTarget: (key: HighThroughputTargetKey) => void;
  uploads: PriorDataUploadsState;
  onUpload: (key: HighThroughputTargetKey, file: File | null) => void;
  onBack: () => void;
  onNext: () => void;
  canAdvance: boolean;
  transitionMessage: string | null;
  candidateMap: ReactNode;
};

// Narrow canvases show one floating card at a time to keep its controls reachable.
const COMPACT_AGENT_LAYOUT_WIDTH = 836;

function uploadState(upload?: PriorDataUploadState) {
  if (upload?.errorMessage) return "error";
  if (upload?.isLoading) return "loading";
  return upload ? "ready" : "pending";
}

function UploadStatusIcon({ upload }: { upload?: PriorDataUploadState }) {
  const state = uploadState(upload);
  if (state === "ready") return <CheckCircle2 aria-hidden="true" />;
  if (state === "error") return <TriangleAlert aria-hidden="true" />;
  if (state === "loading") return <LoaderCircle className="ht-s1-spinner" aria-hidden="true" />;
  return <Circle aria-hidden="true" />;
}

export function PriorImportWorkspace({
  targets, candidateTotal, materialType, representation, activeTargetKey,
  onSelectTarget, uploads, onUpload, onBack, onNext, canAdvance,
  transitionMessage, candidateMap,
}: PriorImportWorkspaceProps) {
  const [openAgents, setOpenAgents] = useState<Partial<Record<HighThroughputTargetKey, boolean>>>({});
  const layoutRef = useRef<HTMLDivElement>(null);
  const lastOpenTarget = useRef<HighThroughputTargetKey>("tg");
  const activeTarget = targets.find((target) => target.key === activeTargetKey) ?? targets[0];
  const activeUpload = uploads[activeTarget.key];
  const activeCsv = highThroughputDemoScenario.doeCsvFiles[activeTarget.key];
  const readyCount = targets.filter((target) => uploadState(uploads[target.key]) === "ready").length;
  const isBusy = Boolean(transitionMessage);
  const tabId = useId();

  function toggleAgent(targetKey: HighThroughputTargetKey) {
    const willOpen = !openAgents[targetKey];
    const width = layoutRef.current?.clientWidth ?? 0;
    if (willOpen) {
      lastOpenTarget.current = targetKey;
      onSelectTarget(targetKey);
    }
    setOpenAgents((agents) => willOpen && width > 0 && width < COMPACT_AGENT_LAYOUT_WIDTH
      ? { [targetKey]: true }
      : { ...agents, [targetKey]: willOpen });
  }

  useEffect(() => {
    if (!layoutRef.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0 && entry.contentRect.width < COMPACT_AGENT_LAYOUT_WIDTH) {
        setOpenAgents((agents) => {
          const openKeys = (Object.keys(agents) as HighThroughputTargetKey[]).filter((key) => agents[key]);
          if (openKeys.length <= 1) return agents;
          const keepKey = agents[lastOpenTarget.current] ? lastOpenTarget.current : openKeys[0];
          return { [keepKey]: true };
        });
      }
    });
    observer.observe(layoutRef.current);
    return () => observer.disconnect();
  }, []);

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % targets.length;
    else if (event.key === "ArrowLeft") nextIndex = (index + targets.length - 1) % targets.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = targets.length - 1;
    else return;
    event.preventDefault();
    onSelectTarget(targets[nextIndex].key);
    document.getElementById(`${tabId}-${targets[nextIndex].key}`)?.focus();
  }

  return (
    <div className="ht-s1-stage">
      <div className="ht-s1-content">
        <div className="ht-workbench-demo-note">
          <BadgeInfo aria-hidden="true" size={16} />
          <span><strong>演示说明：</strong>使用四份配套 CSV 样例演示先验导入；当前按文件名匹配预设数据，不解析上传内容，也不启动真实实验。</span>
        </div>

        <div className="ht-s1-summary">
          <div className="ht-s1-scenario-facts">
            <Layers3 aria-hidden="true" />
            <span>{materialType}</span>
            <span><b>{candidateTotal.toLocaleString("en-US")}</b> 候选</span>
            <span>{representation} · 2D</span>
          </div>
          <div className="ht-s1-import-progress" role="status" aria-live="polite">
            <span>先验就绪 <b>{readyCount} / {targets.length}</b></span>
            <span className="ht-s1-progress-track" aria-hidden="true">
              {targets.map((target) => <i key={target.key} className={cn(uploadState(uploads[target.key]) === "ready" && "ready")} />)}
            </span>
          </div>
        </div>

        <div ref={layoutRef} className="ht-s1-agent-layout">
          {targets.map((target, index) => (
            <PriorAgentDisclosure
              key={target.key}
              target={target}
              index={index}
              open={Boolean(openAgents[target.key])}
              onToggle={() => toggleAgent(target.key)}
              selected={activeTargetKey === target.key}
              onSelect={() => onSelectTarget(target.key)}
              upload={uploads[target.key]}
              onUpload={(file) => { onSelectTarget(target.key); onUpload(target.key, file); }}
              disabled={isBusy}
            />
          ))}

          <section className="ht-s1-space-panel" aria-labelledby={`${tabId}-space-heading`}>
            <div className="ht-s1-section-title">
              <span className="ht-s1-section-index">01</span>
              <div>
                <h3 id={`${tabId}-space-heading`}>候选空间</h3>
                <p>切换目标，查看对应 DOE 样本分布</p>
              </div>
            </div>
            <div className="ht-s1-target-tabs" role="tablist" aria-label="切换先验目标">
              {targets.map((target, index) => (
                <button
                  key={target.key}
                  id={`${tabId}-${target.key}`}
                  type="button"
                  role="tab"
                  aria-selected={target.key === activeTargetKey}
                  aria-controls={`${tabId}-space`}
                  tabIndex={target.key === activeTargetKey ? 0 : -1}
                  onClick={() => onSelectTarget(target.key)}
                  onKeyDown={(event) => handleTabKeyDown(event, index)}
                  disabled={isBusy}
                  style={{ "--target-color": target.color } as CSSProperties}
                >
                  <UploadStatusIcon upload={uploads[target.key]} />
                  {target.shortLabel}
                </button>
              ))}
            </div>
            <div className="ht-s1-map" id={`${tabId}-space`} role="tabpanel" aria-labelledby={`${tabId}-${activeTargetKey}`} tabIndex={0}>
              {candidateMap}
            </div>
            <div className="ht-s1-map-legend" style={{ "--target-color": activeTarget.color } as CSSProperties}>
              <span><i className="candidate" aria-hidden="true" />候选点</span>
              <span><i className="doe" aria-hidden="true" />DOE 样本 <b>{uploadState(activeUpload) === "ready" ? activeCsv.rows.length : 0}</b></span>
              <span className="ht-s1-map-coordinate">{representation} 二维投影</span>
            </div>
          </section>

        </div>

        <PriorDataPreview key={activeTarget.key} target={activeTarget} csv={activeCsv} upload={activeUpload} />
      </div>

      <footer className="ht-s1-footer">
        <button type="button" className="ht-s1-secondary-button" onClick={onBack} disabled={isBusy}>
          <ArrowLeft aria-hidden="true" />返回 S0 场景设置
        </button>
        <div className="ht-s1-next-action">
          <p role="status" aria-live="polite">
            {transitionMessage || (canAdvance ? "四份先验已就绪，可以进入热点分析" : `还需上传 ${targets.length - readyCount} 份配套 CSV`)}
          </p>
          <button type="button" className="ht-s1-primary-button" onClick={onNext} disabled={!canAdvance || isBusy}>
            {isBusy ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : null}
            {isBusy ? "正在生成先验热点" : "确认先验，进入 S2"}
            {!isBusy ? <ChevronRight aria-hidden="true" /> : null}
          </button>
        </div>
      </footer>
    </div>
  );
}

function PriorAgentDisclosure({
  target, index, open, onToggle, selected, onSelect, upload, onUpload, disabled,
}: {
  target: HighThroughputTarget;
  index: number;
  open: boolean;
  onToggle: () => void;
  selected: boolean;
  onSelect: () => void;
  upload?: PriorDataUploadState;
  onUpload: (file: File | null) => void;
  disabled: boolean;
}) {
  const panelId = useId();
  const toggleRef = useRef<HTMLButtonElement>(null);
  const buttonLabel = `${open ? "收起" : "展开"} ${target.shortLabel} Agent`;
  const ToggleIcon = index < 2
    ? (open ? ChevronRight : ChevronLeft)
    : (open ? ChevronLeft : ChevronRight);

  function handleToggle() {
    onToggle();
    toggleRef.current?.focus({ preventScroll: true });
  }

  return (
    <aside
      className={cn("ht-s1-agent-disclosure", index < 2 ? "left" : "right", open && "expanded", selected && "selected")}
      aria-label={`${target.shortLabel} Agent 面板`}
      style={{ "--target-color": target.color, "--agent-column": index < 2 ? 1 : 3, "--agent-row": index % 2 + 1 } as CSSProperties}
    >
      <button
        ref={toggleRef}
        type="button"
        className="ht-s1-agent-toggle"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={buttonLabel}
        title={buttonLabel}
        onClick={handleToggle}
        disabled={disabled && !open}
      >
        <ToggleIcon aria-hidden="true" />
      </button>
      <div className="ht-s1-agent-flyout np-sw-accented-surface" id={panelId} hidden={!open}>
        <PriorAgentCard
          selectId={`${panelId}-select`}
          index={index}
          target={target}
          upload={upload}
          selected={selected}
          onSelect={onSelect}
          onUpload={onUpload}
          disabled={disabled}
        />
      </div>
    </aside>
  );
}

function PriorAgentCard({ selectId, index, target, upload, selected, onSelect, onUpload, disabled }: {
  selectId: string;
  index: number;
  target: HighThroughputTarget;
  upload?: PriorDataUploadState;
  selected: boolean;
  onSelect: () => void;
  onUpload: (file: File | null) => void;
  disabled: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const statusId = useId();
  const csv = highThroughputDemoScenario.doeCsvFiles[target.key];
  const state = uploadState(upload);
  const statusText = state === "ready" ? "先验已就绪" : state === "loading" ? "正在载入样例…" : state === "error" ? "文件不匹配" : "等待导入";
  const DirectionIcon = target.direction === "higher" ? ArrowUpRight : ArrowDownRight;
  const unitLabel = target.unit === "degC" ? "°C" : target.unit;

  return (
    <article className={cn("ht-s1-agent-card", selected && "selected")} style={{ "--target-color": target.color } as CSSProperties}>
      <div className="ht-s1-agent-meta">
        <span className="ht-s1-agent-id">AGENT <b>{String(index + 1).padStart(2, "0")}</b></span>
        <span className="ht-s1-agent-view-label">{selected ? "当前查看" : "先验导入"}</span>
      </div>
      <button type="button" id={selectId} className="ht-s1-agent-select" onClick={onSelect} aria-pressed={selected} aria-label={`查看 ${target.shortLabel} Agent`} disabled={disabled}>
        <span className="ht-s1-agent-icon"><Bot aria-hidden="true" /></span>
        <span><strong>{target.shortLabel} Agent</strong><small>{target.label}</small></span>
        <ChevronRight aria-hidden="true" />
      </button>
      <div className="ht-s1-agent-target">
        <div className="ht-s1-agent-target-label">
          <span>目标阈值</span>
          <small><DirectionIcon aria-hidden="true" />{target.direction === "higher" ? "越高越好" : "越低越好"}</small>
        </div>
        <strong><span className="ht-s1-target-operator">{target.direction === "higher" ? "≥" : "≤"}</span> {target.key === "modulus" ? target.target.toFixed(1) : target.target} <small>{unitLabel}</small></strong>
      </div>
      <div className={cn("ht-s1-upload-area", state)}>
        <input
          ref={inputRef}
          type="file"
          accept=".csv"
          aria-label={`上传 ${target.shortLabel} CSV 文件`}
          hidden
          disabled={disabled}
          onChange={(event) => {
            const file = event.currentTarget.files?.[0];
            if (file) onUpload(file);
            event.currentTarget.value = "";
          }}
        />
        <div className="ht-s1-file-summary">
          <FileSpreadsheet aria-hidden="true" />
          <span className="ht-s1-file-name" title={upload?.fileName ?? csv.fileName}>{upload?.fileName ?? csv.fileName}</span>
        </div>
        <div className="ht-s1-upload-meta">
          <span>{csv.rows.length} 条 DOE 样本</span>
          <a href={csv.href} download={csv.fileName} aria-label={`下载 ${target.shortLabel} 样例 CSV`}><Download aria-hidden="true" />样例</a>
        </div>
        <button type="button" className="ht-s1-upload-button" onClick={() => inputRef.current?.click()} aria-describedby={statusId} disabled={disabled}>
          {state === "loading" ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : <UploadCloud aria-hidden="true" />}
          {upload ? `重新上传 ${target.shortLabel} CSV` : `上传 ${target.shortLabel} CSV`}
        </button>
      </div>
      <div className={cn("ht-s1-agent-status", state)} id={statusId} role={state === "error" ? "alert" : "status"}>
        <UploadStatusIcon upload={upload} /><span>{statusText}{state === "error" ? "，请使用同名样例" : ""}</span>
      </div>
    </article>
  );
}

function PriorDataPreview({ target, csv, upload }: {
  target: HighThroughputTarget;
  csv: HighThroughputDoeCsvFile;
  upload?: PriorDataUploadState;
}) {
  const state = uploadState(upload);
  const titleId = useId();
  const tableRef = useRef<HTMLDivElement>(null);
  const [showStructure, setShowStructure] = useState(false);
  const fullTableId = useId();

  function toggleStructure() {
    if (showStructure && tableRef.current) tableRef.current.scrollLeft = 0;
    setShowStructure((show) => !show);
  }

  return (
    <section className="ht-s1-preview" aria-labelledby={titleId} style={{ "--target-color": target.color } as CSSProperties}>
      <div className="ht-s1-preview-header">
        <div className="ht-s1-section-title">
          <span className="ht-s1-section-index">02</span>
          <div><h3 id={titleId}>{target.shortLabel} 先验数据预览</h3><p>{state === "ready" ? `预设样例 · ${csv.rows.length} 条记录 · ${csv.propertyColumn}` : "上传后显示配套样例数据"}</p></div>
        </div>
        {state === "ready" ? (
          <button type="button" className="ht-s1-detail-toggle" aria-expanded={showStructure} aria-controls={fullTableId} onClick={toggleStructure}>
            {showStructure ? "收起结构字段" : "展开结构字段"}<ChevronDown aria-hidden="true" />
          </button>
        ) : null}
      </div>

      {state === "ready" ? (
        <div className="ht-s1-data-scroll" ref={tableRef} tabIndex={0} role="region" aria-label={`${target.shortLabel} 先验数据表，可滚动`}>
          <table className={cn("ht-s1-data-table", showStructure && "with-structure")} id={fullTableId}>
            <caption className="sr-only">{target.shortLabel} 配套 CSV 预设数据，不代表对上传文件内容的解析结果</caption>
            <thead><tr>
              <th scope="col">DOE</th><th scope="col">候选 ID</th><th scope="col">单体组合</th>
              <th scope="col">PolyBERT X / Y</th><th scope="col">{target.shortLabel} ({target.unit})</th>
              {showStructure ? <><th scope="col">PI 来源 ID</th><th scope="col">Cluster</th><th scope="col">Polymer SMILES</th><th scope="col">单体 A SMILES</th><th scope="col">单体 B SMILES</th></> : null}
            </tr></thead>
            <tbody>{csv.rows.map((row) => <tr key={row.candidateId}>
              <td>{row.doeRun}</td><td><strong>{row.candidateId}</strong></td><td>{row.monomerA} / {row.monomerB}</td>
              <td>{row.polybertX.toFixed(2)} / {row.polybertY.toFixed(2)}</td>
              <td className="ht-s1-property-value">{row.propertyValue}</td>
              {showStructure ? <><td>{row.sourcePiId ?? "—"}</td><td>{row.cluster}</td><td className="smiles">{row.polymerSmiles}</td><td className="smiles">{row.monomerASmiles}</td><td className="smiles">{row.monomerBSmiles}</td></> : null}
            </tr>)}</tbody>
          </table>
        </div>
      ) : (
        <div className={cn("ht-s1-preview-empty", state)} aria-busy={state === "loading"}>
          {state === "loading" ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : state === "error" ? <TriangleAlert aria-hidden="true" /> : <FileSpreadsheet aria-hidden="true" />}
          <div><strong>{state === "loading" ? "正在载入 DOE 样本" : state === "error" ? "暂时无法展示先验数据" : `等待导入 ${target.shortLabel} 先验`}</strong>
            <p>{state === "error" ? `请在 Agent 卡片中重新上传 ${csv.fileName}` : `通过两侧 Agent 卡片上传 ${csv.fileName}`}</p>
          </div>
          <a href={csv.href} download={csv.fileName}><Download aria-hidden="true" />下载当前样例</a>
        </div>
      )}
    </section>
  );
}
