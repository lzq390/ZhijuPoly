import {
  ArrowLeft, BadgeInfo, CheckCircle2, ChevronDown, ChevronRight, Circle, Download,
  FileSpreadsheet, Layers3, LoaderCircle, UploadCloud, TriangleAlert,
} from "lucide-react";
import { type CSSProperties, type ReactNode, useId, useRef, useState } from "react";
import {
  highThroughputDemoScenario,
  type HighThroughputDoeCsvFile,
  type HighThroughputTarget,
  type HighThroughputTargetKey,
} from "../../constants/highThroughputDemoScenario";
import { cn } from "../../lib/utils";
import type { PriorDataUploadsState, PriorDataUploadState } from "./types";
import { AgentCardShell, AgentDockLayout, AGENT_PROPERTY_ICONS, TargetTabs } from "./PriorWorkbench";

type PriorImportWorkspaceProps = {
  targets: HighThroughputTarget[];
  candidateTotal: number;
  materialType: string;
  representation: string;
  activeTargetKey: HighThroughputTargetKey;
  onSelectTarget: (key: HighThroughputTargetKey) => void;
  uploads: PriorDataUploadsState;
  onUpload: (key: HighThroughputTargetKey, file: File | null) => void;
  onUploadSample: (key: HighThroughputTargetKey) => void;
  onBack: () => void;
  onNext: () => void;
  canAdvance: boolean;
  transitionMessage: string | null;
  candidateMap: ReactNode;
};

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
  onSelectTarget, uploads, onUpload, onUploadSample, onBack, onNext, canAdvance,
  transitionMessage, candidateMap,
}: PriorImportWorkspaceProps) {
  const mapRef = useRef<HTMLDivElement>(null);
  const activeTarget = targets.find((target) => target.key === activeTargetKey) ?? targets[0];
  const activeUpload = uploads[activeTarget.key];
  const activeCsv = highThroughputDemoScenario.doeCsvFiles[activeTarget.key];
  const ActivePropertyIcon = AGENT_PROPERTY_ICONS[activeTarget.key];
  const readyCount = targets.filter((target) => uploadState(uploads[target.key]) === "ready").length;
  const isBusy = Boolean(transitionMessage);
  const tabId = useId();


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
              {targets.map((target) => <i key={target.key} className={cn(uploadState(uploads[target.key]) === "ready" && "ready")} style={{ "--target-color": target.color } as CSSProperties} />)}
            </span>
          </div>
        </div>

        <AgentDockLayout targets={targets} activeTargetKey={activeTargetKey} onSelectTarget={onSelectTarget}
          disabled={isBusy} mapRef={mapRef}
          renderAgent={(props) => (
            <PriorAgentCard {...props} upload={uploads[props.target.key]}
              onUpload={(file) => { onSelectTarget(props.target.key); onUpload(props.target.key, file); }}
              onUploadSample={() => { onSelectTarget(props.target.key); onUploadSample(props.target.key); }} />
          )}>
          <section className="ht-s1-space-panel" aria-labelledby={`${tabId}-space-heading`}>
            <header className="ht-s1-space-header">
              <Layers3 className="ht-s1-space-watermark" aria-hidden="true" />
              <div className="ht-s1-section-title">
                <span className="ht-s1-section-index">01</span>
                <div>
                  <h3 id={`${tabId}-space-heading`}>候选空间</h3>
                  <p>切换目标，查看对应 DOE 样本分布</p>
                </div>
              </div>
              <div className="ht-s1-space-focus" style={{ "--target-color": activeTarget.color } as CSSProperties}>
                <ActivePropertyIcon aria-hidden="true" />
                <span>当前目标 <strong>{activeTarget.shortLabel}</strong></span>
              </div>
            </header>
            <div className="ht-s1-space-body">
              <TargetTabs targets={targets} activeTargetKey={activeTargetKey} onSelectTarget={onSelectTarget}
                disabled={isBusy} tabId={tabId} panelId={`${tabId}-space`} label="切换先验目标"
                renderIcon={(target) => <UploadStatusIcon upload={uploads[target.key]} />} />
              <div ref={mapRef} className="ht-s1-map" id={`${tabId}-space`} role="tabpanel" aria-labelledby={`${tabId}-${activeTargetKey}`} tabIndex={0}>
                {candidateMap}
              </div>
              <div className="ht-s1-map-legend" style={{ "--target-color": activeTarget.color } as CSSProperties}>
                <span><i className="candidate" aria-hidden="true" />候选点</span>
                <span><i className="doe" aria-hidden="true" />DOE 样本 <b>{uploadState(activeUpload) === "ready" ? activeCsv.rows.length : 0}</b></span>
                <span className="ht-s1-map-coordinate">{representation} 二维投影</span>
              </div>
            </div>
          </section>

        </AgentDockLayout>

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

function PriorAgentCard({ selectId, index, target, upload, selected, onSelect, onUpload, onUploadSample, disabled }: {
  selectId: string;
  index: number;
  target: HighThroughputTarget;
  upload?: PriorDataUploadState;
  selected: boolean;
  onSelect: () => void;
  onUpload: (file: File | null) => void;
  onUploadSample: () => void;
  disabled: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const statusId = useId();
  const state = uploadState(upload);
  const statusText = state === "ready" ? "先验已就绪" : state === "loading" ? "载入中…" : state === "error" ? "上传失败：文件不匹配" : "未上传";
  const uploadHint = state === "ready" ? `已上传 · ${upload?.sampleCount ?? 0} 条 DOE 样本`
    : state === "loading" ? "正在准备先验数据"
    : state === "error" ? "请重新上传或使用样例"
    : "上传 CSV 或使用样例";
  const FileIcon = state === "pending" ? UploadCloud : state === "error" ? TriangleAlert : FileSpreadsheet;

  return (
    <AgentCardShell selectId={selectId} index={index} target={target} selected={selected}
      onSelect={onSelect} disabled={disabled} stageLabel="先验导入">
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
            <FileIcon aria-hidden="true" />
            {upload ? (
              <span className="ht-s1-file-name" title={upload.fileName}>{upload.fileName}</span>
            ) : (
              <span className="ht-s1-file-placeholder">尚未上传先验数据</span>
            )}
          </div>
          <div className="ht-s1-upload-meta">
            <span>{uploadHint}</span>
          </div>
          <div className="ht-s1-upload-actions">
            <button
              type="button"
              className="ht-s1-upload-button"
              onClick={() => inputRef.current?.click()}
              aria-label={`${upload ? "重新上传 CSV" : "上传 CSV"}（${target.shortLabel}）`}
              aria-describedby={statusId}
              disabled={disabled}
            >
              {state === "loading" ? <LoaderCircle className="ht-s1-spinner" aria-hidden="true" /> : <UploadCloud aria-hidden="true" />}
              <span>{upload ? "重新上传 CSV" : "上传 CSV"}</span>
            </button>
            <button
              type="button"
              className="ht-s1-sample-button"
              onClick={onUploadSample}
              aria-label={`上传样例（${target.shortLabel}）`}
              aria-describedby={statusId}
              title={`直接载入 ${target.shortLabel} 配套样例，无需下载`}
              disabled={disabled || state === "loading"}
            >上传样例</button>
          </div>
        </div>
        <div className={cn("ht-s1-agent-status", state)} id={statusId} role={state === "error" ? "alert" : "status"}>
          <UploadStatusIcon upload={upload} /><span>{statusText}</span>
        </div>
    </AgentCardShell>
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
