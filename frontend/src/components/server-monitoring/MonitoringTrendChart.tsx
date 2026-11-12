import { useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { MonitoringHistory, MonitoringHistoryPoint } from "../../types/serverMonitoring";
import "./monitoring-trend-chart.css";

export type TrendSeries = { key: string; label: string; color: string; value: (point: MonitoringHistoryPoint) => number | null };
type ChartPoint = { x: number; y: number };
const chartWidth = (width: number) => Number.isFinite(width) && width > 0 ? Math.max(280, width) : 920;

/** Null samples and missed intervals remain visible gaps, never zero-filled or bridged. */
export function buildTrendSegments(history: MonitoringHistory, series: TrendSeries, width = 920): ChartPoint[][] {
  const end = Date.parse(history.captured_at), start = end - 3_600_000;
  const plotWidth = chartWidth(width) - 84;
  const segments: ChartPoint[][] = [];
  let segment: ChartPoint[] = [];
  let priorTime: number | null = null;
  const flush = () => { if (segment.length) segments.push(segment); segment = []; };
  const samples = history.points.filter(point => Number.isFinite(Date.parse(point.timestamp)))
    .slice().sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  for (const point of samples) {
    const timestamp = Date.parse(point.timestamp);
    if (timestamp < start || timestamp > end) continue;
    const value = series.value(point);
    if (value == null || !Number.isFinite(value) || value < 0 || value > 100) { flush(); priorTime = null; continue; }
    if (priorTime !== null && timestamp - priorTime > history.interval_seconds * 1_750) flush();
    segment.push({ x: 48 + (timestamp - start) / 3_600_000 * plotWidth, y: 194 - value / 100 * 164 });
    priorTime = timestamp;
  }
  flush();
  return segments;
}
export function monitoringClock(timestamp: string | number | null | undefined) {
  if (timestamp == null || !Number.isFinite(new Date(timestamp).getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(timestamp));
}

export function MonitoringTrendChart({ history, series }: { history: MonitoringHistory | null; series: TrendSeries[] }) {
  if (!history || !history.points.length) return <div className="sm-chart-empty" role="status"><span>等待历史采样</span></div>;
  return <InteractiveTrendChart key={`${history.collector_id}:${history.started_at}`} history={history} series={series} />;
}

function samplePercent(value: number | null) {
  return value != null && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}
function reading(value: number | null) {
  const valid = samplePercent(value);
  return valid == null ? "--" : `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(valid)}%`;
}
function sampleClock(timestamp: string) {
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(timestamp));
}
type Selection = { timestamp: string; mode: "hover" | "touch" | "keyboard" };

function InteractiveTrendChart({ history, series }: { history: MonitoringHistory; series: TrendSeries[] }) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const [width, setWidth] = useState(920);
  const plotRef = useRef<HTMLDivElement>(null);
  const touchStart = useRef<{ id: number; x: number; y: number } | null>(null);
  const hintId = useId(), tooltipId = useId();
  useLayoutEffect(() => {
    const plot = plotRef.current;
    if (!plot) return;
    let active = true;
    const measure = (measured: number) => {
      // Ignore a temporarily hidden container rather than shrinking its last
      // usable layout. Initial/server rendering keeps the 920px fallback.
      if (!active || !Number.isFinite(measured) || measured <= 0) return;
      const next = chartWidth(measured);
      setWidth(previous => Math.abs(previous - next) < 0.25 ? previous : next);
    };
    const measureBounds = () => measure(plot.getBoundingClientRect().width);
    measureBounds();
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(entries => {
        const entry = entries.find(item => item.target === plot);
        if (entry) measure(entry.contentRect.width);
      });
      observer.observe(plot);
      return () => { active = false; observer.disconnect(); };
    }
    window.addEventListener("resize", measureBounds);
    return () => { active = false; window.removeEventListener("resize", measureBounds); };
  }, []);
  const plotWidth = width - 84, right = width - 36, midpoint = 48 + plotWidth / 2;
  const end = Date.parse(history.captured_at);
  const start = end - 3_600_000;
  const samples = history.points.filter(point => {
    const timestamp = Date.parse(point.timestamp);
    return Number.isFinite(timestamp) && timestamp >= start && timestamp <= end;
  }).slice().sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  const selectedIndex = selection ? samples.findIndex(point => point.timestamp === selection.timestamp) : -1;
  const selected = samples[selectedIndex] ?? null;
  const selectedX = selected ? 48 + (Date.parse(selected.timestamp) - start) / 3_600_000 * plotWidth : null;
  const paths = series.map(item => ({ ...item, segments: buildTrendSegments(history, item, width) }));
  const hasValues = paths.some(item => item.segments.length);
  const selectAt = (index: number, mode: Selection["mode"]) => {
    const point = samples[Math.min(samples.length - 1, Math.max(0, index))];
    if (point) setSelection(previous => previous?.timestamp === point.timestamp && previous.mode === mode ? previous : { timestamp: point.timestamp, mode });
  };
  const selectPointer = (event: PointerEvent<SVGSVGElement>, mode: Selection["mode"]) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    // Keep xMidYMid/meet: even during a resize, hit testing uses the same viewBox
    // as the paths without stretching text or assuming client pixels match it.
    const scale = Math.min(bounds.width / width, bounds.height / 238);
    if (!Number.isFinite(scale) || scale <= 0 || !samples.length) return;
    const x = (event.clientX - bounds.left - (bounds.width - width * scale) / 2) / scale;
    const y = (event.clientY - bounds.top - (bounds.height - 238 * scale) / 2) / scale;
    if (x < 48 || x > right || y < 30 || y > 194) {
      if (mode === "hover") setSelection(previous => previous?.mode === "hover" ? null : previous);
      return;
    }
    const targetTime = start + (x - 48) / plotWidth * 3_600_000;
    const nearest = samples.reduce((best, point, index) => Math.abs(Date.parse(point.timestamp) - targetTime) < Math.abs(Date.parse(samples[best].timestamp) - targetTime) ? index : best, 0);
    selectAt(nearest, mode);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") { event.preventDefault(); setSelection(null); return; }
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || !samples.length) return;
    event.preventDefault();
    const current = selectedIndex < 0 ? samples.length - 1 : selectedIndex;
    selectAt(event.key === "Home" ? 0 : event.key === "End" ? samples.length - 1 : current + (event.key === "ArrowLeft" ? -1 : 1), "keyboard");
  };
  return <div className="sm-chart-wrap">
    <div className="sm-chart-legend">{series.map(item => <span key={item.key}><i style={{ background: item.color }} />{item.label}</span>)}</div>
    <div ref={plotRef} className="sm-trend-plot" role="group" aria-label="趋势采样读数" tabIndex={samples.length ? 0 : -1}
      aria-describedby={`${hintId}${selected ? ` ${tooltipId}` : ""}`} onKeyDown={onKeyDown}
      onFocus={event => { if (event.target === event.currentTarget && !selected && !touchStart.current) selectAt(samples.length - 1, "keyboard"); }}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setSelection(null); }}>
    <span id={hintId} className="sm-trend-sr-only">左右方向键查看相邻采样，Home 和 End 查看首末采样，Escape 关闭读数。</span>
    <svg className="sm-chart" viewBox={`0 0 ${width} 238`} role="img" aria-label={`最近一小时${series.map(item => item.label).join("、")}趋势，纵轴 0 至 100%，缺失采样处断开`}
      onPointerMove={event => { if (event.pointerType !== "touch") selectPointer(event, "hover"); }}
      onPointerLeave={() => setSelection(previous => previous?.mode === "hover" ? null : previous)}
      onPointerDown={event => { if (event.pointerType === "touch") touchStart.current = { id: event.pointerId, x: event.clientX, y: event.clientY }; }}
      onPointerUp={event => {
        const touch = touchStart.current; touchStart.current = null;
        if (event.pointerType === "touch" && touch?.id === event.pointerId && Math.hypot(event.clientX - touch.x, event.clientY - touch.y) <= 10) selectPointer(event, "touch");
      }}
      onPointerCancel={() => { touchStart.current = null; }}>
      <text x="48" y="17" className="sm-chart-unit">使用率 %</text>
      {[0, 25, 50, 75, 100].map(value => <g key={value}>
        <line x1="48" y1={194 - value / 100 * 164} x2={right} y2={194 - value / 100 * 164} className="sm-chart-grid" />
        <text x="36" y={198 - value / 100 * 164} textAnchor="end" className="sm-chart-tick">{value}</text>
      </g>)}
      {[0, 1, 2, 3, 4].map(index => <line key={`vertical-${index}`} x1={48 + index * plotWidth / 4} x2={48 + index * plotWidth / 4} y1="30" y2="194" className="sm-chart-grid sm-chart-grid-vertical" />)}
      {[0, 1, 2, 3, 4].map(index => <text key={index} x={48 + index * plotWidth / 4} y="222" textAnchor={index === 0 ? "start" : index === 4 ? "end" : "middle"} className="sm-chart-tick">{monitoringClock(end - (4 - index) * 900_000)}</text>)}
      {paths.map(item => <g key={item.key} stroke={item.color} fill="none" strokeWidth="2.4" strokeLinejoin="round" strokeLinecap="round">
        {item.segments.map((segment, index) => segment.length === 1
          ? <circle key={index} cx={segment[0].x} cy={segment[0].y} r="2.5" fill={item.color} stroke="none" />
          : <path key={index} d={segment.map((point, at) => `${at ? "L" : "M"}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(" ")} />)}
      </g>)}
      {selected && selectedX !== null && <g className="sm-trend-cursor" aria-hidden="true">
        <line className="sm-trend-crosshair" x1={selectedX} x2={selectedX} y1="30" y2="194" />
        {series.map(item => {
          const value = samplePercent(item.value(selected));
          return value === null ? null : <circle key={item.key} className="sm-trend-marker" cx={selectedX} cy={194 - value / 100 * 164} r="4" stroke={item.color} />;
        })}
      </g>}
      {!hasValues && <text x={midpoint} y="118" textAnchor="middle" className="sm-chart-tick">当前区间暂无有效采样</text>}
    </svg>
    {selected && <div id={tooltipId} role="tooltip" className="sm-trend-tooltip" data-side={(selectedX ?? 0) > midpoint ? "left" : "right"}
      aria-live={selection?.mode === "hover" ? "off" : "polite"} aria-atomic="true">
      <time dateTime={selected.timestamp}>{sampleClock(selected.timestamp)}</time>
      <dl>{series.map(item => <div key={item.key}><dt><i style={{ background: item.color }} />{item.label}</dt><dd>{reading(item.value(selected))}</dd></div>)}</dl>
    </div>}
    </div>
  </div>;
}
