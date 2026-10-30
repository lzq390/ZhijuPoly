import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { getSessionEpoch, onSessionRetired } from "../../auth/session";
import { fetchPolymerMdStageLogs, type PolymerMdLogSource, type PolymerMdStageLogs as StageLogResponse } from "../../services/polymerMdApi";
import { boundedPolymerMdRead, POLYMER_MD_LOG_TIMEOUT_MS, PolymerMdAnalysisTimeout, polymerMdReadError } from "../../services/polymerMdAnalysisRequest";
import type { PolymerMdStage } from "../../types/polymerMd";
import { stageLabel } from "./config";
import { PolymerMdSelect } from "./PolymerMdSelect";

const SOURCES: Array<{ value: PolymerMdLogSource; label: string }> = [
  { value: "auto", label: "自动选择" }, { value: "engine", label: "引擎日志" },
  { value: "stdout", label: "程序输出" }, { value: "runner", label: "阶段执行摘要" }
];
const readTime = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const sourceLabel = (value: unknown) => typeof value === "string" && /^[^/\\\r\n]{1,220}$/.test(value) ? value : null;
type Snapshot = StageLogResponse & { readAt: string };

export function PolymerMdStageLogs({ jobId, stage, available }: { jobId: string; stage: PolymerMdStage; available: boolean }) {
  const [open, setOpen] = useState(false), [refresh, setRefresh] = useState(0);
  const [source, setSource] = useState<PolymerMdLogSource>("auto");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null), [loading, setLoading] = useState(false);
  const identity = useRef(getSessionEpoch());
  const saved = snapshot?.job_id === jobId && snapshot.stage_run_id === stage.stage_run_id ? snapshot : null;
  useEffect(() => { setSource("auto"); setSnapshot(null); setError(null); }, [jobId, stage.stage_run_id]);
  useEffect(() => {
    if (!open || !available || identity.current !== getSessionEpoch()) return;
    let disposed = false, controller: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const epoch = getSessionEpoch();
    const retire = onSessionRetired(() => { disposed = true; controller?.abort(); if (timer) clearTimeout(timer); setSnapshot(null); setError(null); setLoading(false); });
    async function load() {
      const active = new AbortController();
      controller = active;
      setLoading(true);
      setError(null);
      try {
        const value = await boundedPolymerMdRead(active, POLYMER_MD_LOG_TIMEOUT_MS, signal => source === "auto"
          ? fetchPolymerMdStageLogs(jobId, stage.stage_run_id, signal)
          : fetchPolymerMdStageLogs(jobId, stage.stage_run_id, signal, { source }));
        if (disposed || active.signal.aborted || epoch !== getSessionEpoch()) return;
        if (value.job_id !== jobId || value.stage_run_id !== stage.stage_run_id || !Array.isArray(value.lines) || !value.lines.every(line => typeof line === "string")) throw new Error("日志与当前任务阶段不匹配。");
        if (source !== "auto" && value.resolved_source && value.resolved_source !== source) throw new Error("日志来源与当前选择不匹配。");
        setSnapshot({ ...value, readAt: value.read_at && Number.isFinite(Date.parse(value.read_at)) ? value.read_at : new Date().toISOString() });
        setError(null);
      } catch (e) {
        if (!disposed && epoch === getSessionEpoch() && (!active.signal.aborted || e instanceof PolymerMdAnalysisTimeout)) setError(polymerMdReadError(e, "日志读取"));
      } finally {
        if (!disposed && epoch === getSessionEpoch()) {
          setLoading(false);
          if (stage.status === "running") timer = setTimeout(() => void load(), 5000);
        }
      }
    }
    void load();
    return () => { disposed = true; retire(); controller?.abort(); if (timer) clearTimeout(timer); };
  }, [jobId, stage.stage_run_id, stage.status, open, refresh, available, source]);
  if (!available) return <p className="np-polymer-md-muted">阶段日志已清理。</p>;
  return <details className="np-polymer-md-logs" onToggle={event => setOpen(event.currentTarget.open)}><summary>查看{stageLabel(stage.stage)}日志</summary>{open ? <>
    <div className="np-polymer-md-actions np-pmd-log-controls"><PolymerMdSelect label="日志来源" value={source} options={SOURCES.map(option => {
      const actual = Array.isArray(saved?.available_sources) ? saved.available_sources.find(item => item?.source === option.value) : undefined;
      const label = sourceLabel(actual?.label);
      return { ...option, label: label ? `${option.label} · ${label}` : option.label };
    })} onChange={value => setSource(value as PolymerMdLogSource)} /><button type="button" aria-label="刷新阶段日志" disabled={loading} onClick={() => setRefresh(value => value + 1)}><RefreshCw aria-hidden="true" />刷新</button></div>
    <p className="np-pmd-log-provenance"><span>末尾 200 行{saved?.truncated ? " · 更早内容已截断" : ""}</span>{saved ? <><span>来源：<span className={sourceLabel(saved.source_label) ? "np-pmd-code" : undefined}>{sourceLabel(saved.source_label) || "来源未记录"}</span></span><span>最近读取：<time dateTime={saved.readAt}>{readTime.format(Date.parse(saved.readAt))}</time> 北京时间{loading ? " · 正在读取，保留上次日志" : error ? " · 已保留上次日志" : ""}</span></> : null}</p>
    {error ? <p role="alert">{error}</p> : null}
    {saved?.lines.length ? <pre tabIndex={0}>{saved.lines.join("\n")}</pre> : <p className="np-pmd-log-empty" role="status">{loading ? "正在读取阶段日志…" : saved?.available === false ? "该来源暂未提供可读日志。" : "当前阶段暂未产生可读日志。"}</p>}
  </> : null}</details>;
}
