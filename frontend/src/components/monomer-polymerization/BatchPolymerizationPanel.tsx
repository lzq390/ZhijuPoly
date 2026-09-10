import { ChevronDown, Download, FileSpreadsheet, FlaskConical, ListChecks, LoaderCircle, Play, RefreshCw, SlidersHorizontal, Square, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { MonomerPolymerizationStatusResponse, MonomerPolymerizationTargetClass } from "../../types";
import type { BatchImport, BatchImportPreview, BatchMapping } from "../../types/polymerizationBatch";
import { BatchArtifactError, batchUrl, cancelBatchJob, downloadBatchArtifact, previewBatchTables, submitBatchJob, uploadBatchTables } from "../../services/polymerizationBatchApi";
import { isBatchActive, usePolymerizationBatchJob } from "../../hooks/usePolymerizationBatchJob";
import { WorkbenchSelect } from "../structure-workbench/WorkbenchSelect";
import { TARGET_CLASS_LABELS } from "./config";
import "../../styles/monomer-polymerization-batch.css";

const DEFAULT_MAPPING: BatchMapping = { sheet: null, encoding: "utf-8-sig", smiles_column: null, id_column: null, name_column: null };
const STATUS_LABELS: Record<string, string> = { queued: "等待计算", running: "正在计算", cancelling: "正在取消", completed: "已完成", completed_with_errors: "已完成，含错误记录", failed: "运行失败", cancelled: "已取消", expired: "文件已过期" };
const STAGE_LABELS: Record<string, string> = { queued: "排队中", classify: "单体分类", generate: "生成聚合物", export: "整理输出文件", finished: "任务结束" };
const count = (value: number) => value.toLocaleString("zh-CN");
const errorMessage = (error: unknown) => error instanceof Error ? error.message : "操作失败，请稍后重试。";
const MAPPING_LABELS = { smiles_column: "SMILES 列（必填）", id_column: "编号列", name_column: "名称列" } as const;
const ARTIFACT_LABELS: Record<string, string> = { "results.zip": "下载 CSV 结果包", "results.xlsx": "下载 Excel", "input_errors.csv": "下载错误记录" };
type TaskMessage = { jobId: string; message: string };
type TaskAction = { jobId: string; kind: "cancel" | "download"; message: string; artifact?: string };

function BatchImportSummary({ role, imported, mapping, preview, busy }: {
  role: "a" | "b";
  imported: BatchImport;
  mapping: BatchMapping;
  preview: BatchImportPreview | null;
  busy: boolean;
}) {
  const table = imported.tables[role];
  const statistics = preview?.statistics?.tables[role];
  const isExcel = imported.files[role].format === "xlsx";
  const dirty = (Object.keys(mapping) as Array<keyof BatchMapping>).some((key) => mapping[key] !== table.mapping[key]);
  return <>
    <div className="np-batch-import-summary" aria-label={`表 ${role.toUpperCase()} 导入摘要`}>
      <span>SMILES 列：<strong>{mapping.smiles_column || "待选择"}</strong></span>
      {statistics ? <span>{count(statistics.valid_rows)} 条有效 · {count(statistics.invalid_rows)} 条错误</span>
        : <span>{dirty ? "设置已修改，请重新预检" : busy ? "正在预检…" : table.error ? "文件待修正" : "待预检"}</span>}
      {isExcel ? <span>工作表：{mapping.sheet || "默认工作表"}</span> : null}
    </div>
    {table.error ? <p className="np-batch-error" role="alert">{table.error}</p>
      : !mapping.smiles_column ? <p className="np-batch-notice">未能唯一识别 SMILES 列，请在导入设置中选择。</p> : null}
  </>;
}

function BatchImportSettings({ role, imported, mapping, preview, busy, onChange }: {
  role: "a" | "b";
  imported: BatchImport;
  mapping: BatchMapping;
  preview: BatchImportPreview | null;
  busy: boolean;
  onChange: (update: Partial<BatchMapping>) => void;
}) {
  const table = imported.tables[role];
  const statistics = preview?.statistics?.tables[role];
  const isExcel = imported.files[role].format === "xlsx";
  const prefix = `np-batch-${role}`;
  const dirty = (Object.keys(mapping) as Array<keyof BatchMapping>).some((key) => mapping[key] !== table.mapping[key]);
  return <section className="np-batch-settings-fields" aria-label={`单体表 ${role.toUpperCase()} 导入设置`}>
        <h3>单体表 {role.toUpperCase()}</h3>
        <div className="np-batch-mapping-field">
          <label htmlFor={`${prefix}-format`}>{isExcel ? "工作表" : "CSV 编码"}</label>
          {isExcel ? <WorkbenchSelect id={`${prefix}-format`} ariaLabel={`${role.toUpperCase()} 工作表`}
            value={mapping.sheet ?? ""} disabled={busy}
            options={table.sheets.map((sheet) => ({ value: sheet, label: sheet }))}
            onChange={(sheet) => onChange({ sheet, smiles_column: null, id_column: null, name_column: null })} />
            : <WorkbenchSelect<BatchMapping["encoding"]> id={`${prefix}-format`} ariaLabel={`${role.toUpperCase()} CSV 编码`}
              value={mapping.encoding} disabled={busy}
              options={[
                { value: "utf-8-sig", label: "UTF-8 / UTF-8 BOM", description: "默认编码，适用于本站模板" },
                { value: "gb18030", label: "GB18030", description: "适用于使用此编码保存的中文 CSV" }
              ]}
              onChange={(encoding) => onChange({ encoding, smiles_column: null, id_column: null, name_column: null })} />}
        </div>
        {(["smiles_column", "id_column", "name_column"] as const).map((field) => (
          <div className="np-batch-mapping-field" key={field}>
            <label htmlFor={`${prefix}-${field}`}>{MAPPING_LABELS[field]}</label>
            <WorkbenchSelect id={`${prefix}-${field}`} ariaLabel={`${role.toUpperCase()} ${MAPPING_LABELS[field]}`}
              value={mapping[field] ?? ""} disabled={busy}
              options={[
                { value: "", label: field === "smiles_column" ? "自动识别" : "不使用", description: field === "smiles_column" ? "无法识别时，请手动选择含 SMILES 的列" : "不关联此字段" },
                ...table.headers.map((header) => ({ value: header, label: header }))
              ]}
              onChange={(value) => onChange({ [field]: field === "smiles_column" ? value || null : value })} />
          </div>
        ))}
        {!dirty && !table.error ? <p className="np-batch-hint">
          {count(table.row_count)} 条记录 · 已跳过 {count(table.blank_rows)} 个空行
          {statistics ? ` · ${count(statistics.unique_count)} 种唯一结构 · ${count(statistics.duplicate_rows)} 条重复结构记录` : ""}
        </p> : null}
    <div className="np-batch-table-preview">
      <h4>{dirty ? "上次预检的前 20 行" : "输入前 20 行"}</h4>
      {table.sample.length ? <div className="np-batch-table-scroll"><table aria-label={`单体表 ${role.toUpperCase()} 前 20 行`}>
        <thead><tr>{table.headers.map((header) => <th key={header}>{header}</th>)}</tr></thead>
        <tbody>{table.sample.map((row, index) => <tr key={index}>{table.headers.map((header) => <td key={header}>{row[header]}</td>)}</tr>)}</tbody>
      </table></div> : <p className="np-batch-hint">{table.error ? "请修正文件后重新预检。" : "没有可显示的输入记录。"}</p>}
    </div>
  </section>;
}

export type BatchPolymerizationSections = {
  inputs: ReactNode;
  settings: ReactNode;
  taskPanel: ReactNode;
};

export function BatchPolymerizationPanel({ status, target, children }: {
  status: MonomerPolymerizationStatusResponse | null;
  target: MonomerPolymerizationTargetClass;
  children: (sections: BatchPolymerizationSections) => ReactNode;
}) {
  const capability = status?.batch;
  const [files, setFiles] = useState<Partial<Record<"a" | "b", File>>>({});
  const [imported, setImported] = useState<BatchImport | null>(null);
  const [preview, setPreview] = useState<BatchImportPreview | null>(null);
  const [mappings, setMappings] = useState<Record<"a" | "b", BatchMapping>>({ a: { ...DEFAULT_MAPPING }, b: { ...DEFAULT_MAPPING } });
  const [importSettingsOpen, setImportSettingsOpen] = useState(false);
  const [taskPanelOpen, setTaskPanelOpen] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [taskAction, setTaskAction] = useState<TaskAction | null>(null);
  const [taskError, setTaskError] = useState<TaskMessage | null>(null);
  const [taskNotice, setTaskNotice] = useState<TaskMessage | null>(null);
  const [unavailableArtifacts, setUnavailableArtifacts] = useState<{ jobId: string; names: string[] } | null>(null);
  const [submittedRevision, setSubmittedRevision] = useState<string | null>(null);
  const operation = useRef<AbortController | null>(null);
  const taskOperation = useRef<AbortController | null>(null);
  const idempotency = useRef<{ signature: string; key: string } | null>(null);
  const tasks = usePolymerizationBatchJob();
  useEffect(() => () => operation.current?.abort(), []);
  useEffect(() => {
    setTaskAction(null);
    setTaskError(null);
    setTaskNotice(null);
    setUnavailableArtifacts(null);
    return () => {
      taskOperation.current?.abort();
      taskOperation.current = null;
    };
  }, [tasks.jobId]);

  function begin(label: string) {
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setBusy(label);
    setError(null);
    return controller;
  }
  function replaceFile(role: "a" | "b", file?: File) {
    operation.current?.abort();
    setBusy(null);
    setFiles((current) => ({ ...current, [role]: file }));
    setImported(null);
    setImportSettingsOpen(false);
    setPreview(null);
    setSubmittedRevision(null);
    setError(null);
    idempotency.current = null;
  }
  function applyInspection(data: BatchImport) {
    setImported(data);
    setMappings({ a: data.tables.a.mapping, b: data.tables.b.mapping });
    setImportSettingsOpen((current) => current || [data.tables.a, data.tables.b].some((table) => Boolean(table.error || !table.mapping.smiles_column)));
  }
  async function validate(id: string, nextMappings: Record<"a" | "b", BatchMapping>, controller: AbortController) {
    const data = await previewBatchTables(id, nextMappings, controller.signal);
    if (controller.signal.aborted) return;
    applyInspection(data);
    setPreview(data);
  }
  async function upload() {
    if (!files.a || !files.b) return;
    if (capability && [files.a, files.b].some((file) => file.size > capability.limits.file_bytes)) {
      setError(`每个文件不能超过 ${Math.floor(capability.limits.file_bytes / 1024 ** 2)} MiB。`);
      return;
    }
    const controller = begin("上传并预检中…");
    setImported(null);
    setPreview(null);
    setSubmittedRevision(null);
    idempotency.current = null;
    try {
      const data = await uploadBatchTables(files.a, files.b, controller.signal);
      if (controller.signal.aborted) return;
      const next = { a: data.tables.a.mapping, b: data.tables.b.mapping };
      applyInspection(data);
      await validate(data.import_id, next, controller);
    } catch (reason) { if (!controller.signal.aborted) setError(errorMessage(reason)); }
    finally { if (!controller.signal.aborted) setBusy(null); }
  }
  async function revalidate() {
    if (!imported) return;
    const controller = begin("正在预检…");
    setPreview(null);
    setSubmittedRevision(null);
    try { await validate(imported.import_id, mappings, controller); }
    catch (reason) { if (!controller.signal.aborted) setError(errorMessage(reason)); }
    finally { if (!controller.signal.aborted) setBusy(null); }
  }
  function updateMapping(role: "a" | "b", update: Partial<BatchMapping>) {
    setMappings((current) => ({ ...current, [role]: { ...current[role], ...update } }));
    setPreview(null);
    setSubmittedRevision(null);
    idempotency.current = null;
  }
  async function submit() {
    if (busy || !preview?.can_submit || !preview.preview_revision || preview.import_id !== imported?.import_id) return;
    const signature = `${preview.import_id}:${preview.preview_revision}:${target}`;
    if (idempotency.current?.signature !== signature) idempotency.current = { signature, key: Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join("") };
    const controller = begin("正在提交…");
    try {
      const job = await submitBatchJob(preview.import_id, preview.preview_revision, target, idempotency.current.key, controller.signal);
      if (controller.signal.aborted) return;
      tasks.selectJob(job.job_id);
      setTaskPanelOpen(true);
      setSubmittedRevision(signature);
    } catch (reason) { if (!controller.signal.aborted) setError(errorMessage(reason)); }
    finally { if (!controller.signal.aborted) setBusy(null); }
  }
  function beginTaskAction(action: TaskAction) {
    if (taskOperation.current || !tasks.isCurrentJob(action.jobId)) return null;
    const controller = new AbortController();
    taskOperation.current = controller;
    setTaskAction(action);
    setTaskError(null);
    setTaskNotice(null);
    return controller;
  }
  function isCurrentTaskAction(id: string, controller: AbortController) {
    return !controller.signal.aborted && taskOperation.current === controller && tasks.isCurrentJob(id);
  }
  function finishTaskAction(controller: AbortController) {
    if (taskOperation.current === controller) {
      taskOperation.current = null;
      setTaskAction(null);
    }
  }
  async function cancel() {
    const current = tasks.job;
    if (!current || !isBatchActive(current) || current.status === "cancelling") return;
    const id = current.job_id;
    const controller = beginTaskAction({ jobId: id, kind: "cancel", message: "正在请求取消…" });
    if (!controller) return;
    try {
      const cancelled = await cancelBatchJob(id, controller.signal);
      if (!isCurrentTaskAction(id, controller)) return;
      if (cancelled.job_id !== id) throw new Error("返回的任务标识不一致，请刷新后确认任务状态。");
      setTaskNotice({ jobId: id, message: isBatchActive(cancelled) ? "取消请求已发送，请等待任务停止。" : "任务已结束，正在刷新最终状态。" });
      tasks.refresh();
    } catch (reason) {
      if (isCurrentTaskAction(id, controller)) setTaskError({ jobId: id, message: `取消失败：${errorMessage(reason)}` });
    } finally { finishTaskAction(controller); }
  }
  async function download(name: string) {
    const current = tasks.job;
    if (!current?.artifacts[name] || current.status === "expired") return;
    const id = current.job_id;
    if (unavailableArtifacts?.jobId === id && unavailableArtifacts.names.includes(name)) return;
    const controller = beginTaskAction({ jobId: id, kind: "download", artifact: name, message: `正在下载 ${name}…` });
    if (!controller) return;
    let objectUrl: string | null = null;
    try {
      const blob = await downloadBatchArtifact(id, name, controller.signal);
      if (!isCurrentTaskAction(id, controller)) return;
      objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      const completedUrl = objectUrl;
      objectUrl = null;
      window.setTimeout(() => URL.revokeObjectURL(completedUrl), 1000);
      setTaskNotice({ jobId: id, message: `已开始下载 ${name}。` });
    } catch (reason) {
      if (!isCurrentTaskAction(id, controller)) return;
      setTaskError({ jobId: id, message: `下载失败：${errorMessage(reason)}` });
      if (reason instanceof BatchArtifactError && reason.status === 410) {
        if (reason.code === "expired") tasks.markFilesExpired(id);
        else setUnavailableArtifacts((current) => ({ jobId: id, names: [...(current?.jobId === id ? current.names : []), name] }));
      }
    } finally {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      finishTaskAction(controller);
    }
  }
  function refreshTask() {
    setTaskError(null);
    setTaskNotice(null);
    setUnavailableArtifacts(null);
    tasks.refresh();
  }
  const job = tasks.job;
  const currentTaskAction = taskAction?.jobId === tasks.jobId ? taskAction : null;
  const currentTaskError = taskError?.jobId === tasks.jobId ? taskError.message : null;
  const currentTaskNotice = taskNotice?.jobId === tasks.jobId ? taskNotice.message : null;
  const historyIds = Array.from(new Set([...(tasks.jobId ? [tasks.jobId] : []), ...tasks.history]));
  const progress = job ? Math.min(100, Math.round(job.summary.processed_pairs / Math.max(1, job.summary.valid_pairs) * 100)) : 0;
  const alreadySubmitted = preview && submittedRevision === `${preview.import_id}:${preview.preview_revision}:${target}`;
  const inputs = <div className="np-batch-panel">
      {!capability?.available ? <p className="np-batch-notice" role="status"><TriangleAlert size={18} />{capability?.message ?? "正在检查批量服务…"}</p> : null}
      <div className="np-batch-inputs">
        {(["a", "b"] as const).map((role) => <section className="np-batch-file" key={role} aria-label={`单体表 ${role.toUpperCase()}`}>
          <header className="np-mp-monomer__header"><div><span className="np-mp-monomer__index">{role.toUpperCase()}</span><div><h3>单体表 {role.toUpperCase()}</h3><p>MONOMER {role.toUpperCase()}</p></div></div><div className="np-batch-templates"><a href={batchUrl(`/templates/${role}.csv`)} download>CSV 模板</a><a href={batchUrl(`/templates/${role}.xlsx`)} download>Excel 模板</a></div></header>
          <label className="np-batch-upload">选择 CSV / XLSX<input type="file" aria-label={`上传单体表 ${role.toUpperCase()}`} accept=".csv,.xlsx" onChange={(event) => replaceFile(role, event.target.files?.[0])} /></label>
          <span className="np-batch-filename">{files[role]?.name ?? "尚未选择文件"}</span>
          {imported ? <BatchImportSummary role={role} imported={imported} mapping={mappings[role]}
            preview={preview} busy={Boolean(busy)} /> : null}
        </section>)}
      </div>
      {imported ? <div className={`np-batch-import-settings${importSettingsOpen ? " is-open" : ""}`}>
        <button type="button" className="np-batch-settings-toggle" aria-expanded={importSettingsOpen}
          aria-controls="np-batch-import-details" onClick={() => setImportSettingsOpen((current) => !current)}>
          <SlidersHorizontal aria-hidden="true" /><span>导入设置与前 20 行</span><ChevronDown aria-hidden="true" />
        </button>
        {importSettingsOpen ? <div id="np-batch-import-details" className="np-batch-import-details">
          {(["a", "b"] as const).map((role) => <BatchImportSettings key={role} role={role}
            imported={imported} mapping={mappings[role]} preview={preview} busy={Boolean(busy)}
            onChange={(update) => updateMapping(role, update)} />)}
        </div> : null}
      </div> : null}
      <p className="np-batch-hint">每表最多 {count(capability?.limits.max_rows ?? 5000)} 条非空记录，组合不超过 {count(capability?.limits.max_pairs ?? 50000)} 对。SMILES 使用普通单体结构，不含 * 连接点。</p>
      <div className="np-batch-actions"><button type="button" onClick={() => void upload()} disabled={Boolean(busy) || !files.a || !files.b || !capability?.enabled}><FileSpreadsheet size={16} />上传并预检</button>{imported ? <button type="button" onClick={() => void revalidate()} disabled={Boolean(busy)}><RefreshCw size={16} />重新预检</button> : null}</div>
      {preview?.statistics ? <section className="np-batch-validation" aria-label="预检结果">
        <div className="np-batch-metrics"><div><span>原始组合</span><strong>{count(preview.statistics.raw_pairs)}</strong></div><div><span>有效组合</span><strong>{count(preview.statistics.valid_pairs)}</strong></div><div><span>独立计算组合</span><strong>{count(preview.statistics.unique_pairs)}</strong></div></div>
        {preview.input_error_count > 0 ? <details><summary>{preview.input_error_count} 条输入错误，运行时将跳过（显示前 100 条）</summary><ul>{preview.input_errors.map((item, index) => <li key={index}>表 {item.role.toUpperCase()} 第 {item.row_number} 行：{item.message}</li>)}</ul></details> : null}
        {!preview.can_submit ? <p className="np-batch-error">任一表没有有效单体时无法提交，请修正文件。</p> : null}
      </section> : null}
  </div>;
  const settings = <div className="np-batch-panel">
      <div className="np-mp-run-note">
        <FlaskConical aria-hidden="true" />
        <p>生成的候选不代表一定可以合成，也不代表相关性质已经得到验证；请结合实验条件进一步评估。</p>
      </div>
      <div className="np-batch-actions np-mp-form-actions"><button type="button" className="np-sw-primary-button" onClick={() => void submit()} disabled={Boolean(busy) || !preview?.can_submit || preview.import_id !== imported?.import_id || !capability?.available || Boolean(alreadySubmitted)}><Play size={16} />{alreadySubmitted ? "任务已提交" : "开始批量聚合"}</button>{busy ? <span role="status"><LoaderCircle className="np-sw-spin" size={16} />{busy}</span> : null}</div>
      {error ? <p role="alert" className="np-batch-error">{error}</p> : null}
  </div>;
  const taskPanel = <section className={`np-batch-panel np-batch-job np-mp-surface np-sw-accented-surface${taskPanelOpen ? " is-open" : ""}`} aria-label="批量任务">
      <header className="np-batch-job-window-header">
        <div className="np-batch-job-heading">
          <span className="np-batch-job-mark"><ListChecks aria-hidden="true" /></span>
          <div>
            <h3>批量任务</h3>
            {!taskPanelOpen ? <p className="np-batch-job-summary">
              {job ? <><strong>{STATUS_LABELS[job.status]}</strong><span>{TARGET_CLASS_LABELS[job.target_class]} · {isBatchActive(job) ? `${STAGE_LABELS[job.stage] ?? job.stage} · ` : ""}已处理 {count(job.summary.processed_pairs)} / {count(job.summary.valid_pairs)}</span></>
                : tasks.error ? null : tasks.jobId ? "正在读取任务…" : "尚未提交任务"}
              {currentTaskAction ? <span className="np-batch-task-feedback" role="status"><LoaderCircle className="np-sw-spin" size={16} aria-hidden="true" />{currentTaskAction.message}</span> : null}
              {currentTaskError || tasks.error ? <span className="np-batch-task-error" role="alert">{currentTaskError ?? tasks.error}</span> : null}
              {currentTaskNotice ? <span role="status">{currentTaskNotice}</span> : null}
            </p> : null}
          </div>
        </div>
        <button type="button" className="np-batch-job-toggle" aria-label={taskPanelOpen ? "收起批量任务" : "展开批量任务"}
          aria-expanded={taskPanelOpen} aria-controls="np-batch-job-details" onClick={() => setTaskPanelOpen((current) => !current)}>
          <span>{taskPanelOpen ? "收起" : "展开"}</span><ChevronDown aria-hidden="true" />
        </button>
      </header>
      <div id="np-batch-job-details" className="np-batch-job-body" hidden={!taskPanelOpen}>
      {taskPanelOpen ? <>
      <div className="np-batch-job-header">
        {historyIds.length ? <div className="np-batch-mapping-field">
          <label htmlFor="np-batch-history">最近任务</label>
          <WorkbenchSelect id="np-batch-history" ariaLabel="最近任务" value={tasks.jobId ?? ""}
            options={historyIds.map((id) => ({ value: id, label: id.slice(0, 12) }))}
            onChange={tasks.selectJob} />
        </div> : null}
        <button type="button" onClick={refreshTask} disabled={!tasks.jobId || tasks.refreshing || Boolean(currentTaskAction)} aria-busy={tasks.refreshing}>
          <RefreshCw size={15} className={tasks.refreshing ? "np-sw-spin" : undefined} aria-hidden="true" />
          {tasks.refreshing ? "刷新中…" : "刷新状态"}
        </button>
      </div>
      {tasks.error ? <p role="alert" className="np-batch-error">{tasks.error}</p> : null}
      {currentTaskError ? <p role="alert" className="np-batch-error">{currentTaskError}</p> : null}
      {currentTaskAction ? <p className="np-batch-hint np-batch-task-feedback" role="status"><LoaderCircle className="np-sw-spin" size={16} aria-hidden="true" />{currentTaskAction.message}</p> : null}
      {currentTaskNotice ? <p className="np-batch-hint" role="status">{currentTaskNotice}</p> : null}
      {job ? <>
        <div className="np-batch-job-status"><strong>{STATUS_LABELS[job.status]}</strong><span>{STAGE_LABELS[job.stage] ?? job.stage} · {TARGET_CLASS_LABELS[job.target_class]}</span><span className="np-batch-job-id">{job.job_id}</span></div>
        {isBatchActive(job) ? <progress value={progress} max={100} aria-label="批量聚合进度" /> : null}
        <p className="np-batch-hint">已处理 {count(job.summary.processed_pairs)} / {count(job.summary.valid_pairs)} 个有效行组合{isBatchActive(job) ? `；${job.stage === "export" ? "正在生成下载文件" : `${progress}%`}。关闭页面后任务继续执行。` : "。"}</p>
        <div className="np-batch-metrics"><div><span>原始组合</span><strong>{count(job.summary.raw_pairs)}</strong></div><div><span>已完成独立计算</span><strong>{count(job.summary.computed_unique_pairs)}</strong></div><div><span>候选结果数</span><strong>{count(job.summary.candidate_count)}</strong></div><div><span>计算错误组合</span><strong>{count(job.summary.pair_errors ?? job.summary.pair_statuses?.error ?? 0)}</strong></div></div>
        <p className="np-batch-hint">一组单体可生成多个候选，候选结果数可能超过组合数。</p>
        {job.message ? <p className="np-batch-error">{job.message}</p> : null}
        {job.summary.pair_statuses ? <p className="np-batch-hint">成功 {count(job.summary.pair_statuses.success ?? 0)} · 无匹配 {count(job.summary.pair_statuses.no_match ?? 0)} · 输入无效 {count(job.summary.pair_statuses.invalid_input ?? 0)} · 计算错误 {count(job.summary.pair_statuses.error ?? 0)} · 未处理 {count(job.summary.pair_statuses.not_processed ?? 0)}</p> : null}
        {job.summary.complete === false && Object.keys(job.artifacts).length ? <p className="np-batch-notice">此任务未完整完成。下载文件仅含已完成结果，pairs 表列出所有未处理组合。</p> : null}
        {job.status === "expired" ? <p className="np-batch-notice">任务文件已过期，无法下载。请重新提交批量任务。</p> : null}
        <div className="np-batch-actions">
          {Object.entries(ARTIFACT_LABELS).map(([name, label]) => job.artifacts[name] ? <button type="button" className="np-batch-download" key={name}
            disabled={Boolean(currentTaskAction) || (unavailableArtifacts?.jobId === job.job_id && unavailableArtifacts.names.includes(name))}
            aria-busy={currentTaskAction?.artifact === name} onClick={() => void download(name)}>
            {currentTaskAction?.artifact === name ? <LoaderCircle className="np-sw-spin" size={16} aria-hidden="true" /> : <Download size={16} aria-hidden="true" />}{label}
          </button> : null)}
          {isBatchActive(job) ? <button type="button" disabled={Boolean(currentTaskAction) || job.status === "cancelling"} onClick={() => void cancel()}><Square size={14} />{job.status === "cancelling" ? "正在取消" : "取消任务"}</button> : null}
        </div>
        {job.finished_at || job.expires_at ? <div className="np-batch-job-times np-batch-hint">
          {job.finished_at ? <span>结束时间：<time dateTime={job.finished_at}>{new Date(job.finished_at).toLocaleString("zh-CN")}</time></span> : null}
          {job.expires_at ? <span>文件到期时间：<time dateTime={job.expires_at}>{new Date(job.expires_at).toLocaleString("zh-CN")}</time></span> : null}
        </div> : null}
      </> : <p className="np-batch-hint">{tasks.error ? "请刷新重试或选择其他任务。" : tasks.jobId ? "正在读取任务…" : "提交后可在此查看进度和下载结果。任务文件默认保存 7 天。"}</p>}
      </> : null}
      </div>
    </section>;
  return children({ inputs, settings, taskPanel });
}
