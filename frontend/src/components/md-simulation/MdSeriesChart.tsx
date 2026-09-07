import { useId, useMemo } from "react";
import type { MdDemoSeries } from "../../types";

function formatNumber(value: number, digits = 3) {
  if (!Number.isFinite(value)) return "--";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(value);
}

export function displayMdUnit(unit: string) {
  if (unit === "A") return "Å";
  if (unit === "g/cm3" || unit === "g/cm^3") return "g/cm³";
  return unit;
}

type GeometryPoint = { x: number; y: number };

function buildGeometry(series: MdDemoSeries) {
  if (!series.points.length) return null;
  const xValues = series.points.map((point) => point.time_ps);
  const yValues = series.points.map((point) => point.value);
  const xMin = Math.min(...xValues);
  const xMax = Math.max(...xValues);
  const yMin = Math.min(...yValues);
  const yMax = Math.max(...yValues);
  const xSpan = xMax - xMin || 1;
  const ySpan = yMax - yMin || 1;
  const points: GeometryPoint[] = series.points.map((point) => ({
    x: 54 + ((point.time_ps - xMin) / xSpan) * 290,
    y: 182 - ((point.value - yMin) / ySpan) * 142
  }));
  const linePath = points.map((point, index) => `${index ? "L" : "M"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ");
  return { points, linePath, xMin, xMax, yMin, yMax };
}

export function MdSeriesChart({
  series,
  color = "#2563eb",
  label = series.label,
}: {
  series: MdDemoSeries;
  color?: string;
  label?: string;
}) {
  const gradientId = `md-series-${useId().replace(/:/g, "")}`;
  const geometry = useMemo(() => buildGeometry(series), [series]);
  const unit = displayMdUnit(series.unit);
  if (!geometry) {
    return (
      <div className="np-md-empty-chart" role="status">
        当前序列没有可显示的数据点。
      </div>
    );
  }
  const yTicks = [0, 0.5, 1].map((ratio) => geometry.yMin + (geometry.yMax - geometry.yMin) * ratio);
  const xTicks = [0, 0.5, 1].map((ratio) => geometry.xMin + (geometry.xMax - geometry.xMin) * ratio);
  const firstPoint = geometry.points[0];
  const lastPoint = geometry.points[geometry.points.length - 1];
  const finalValue = series.points[series.points.length - 1]?.value;

  return (
    <div className="np-md-series-chart">
      <svg viewBox="0 0 360 220" role="img" aria-label={`${label}随时间变化曲线`}>
        <title>{label}随时间变化曲线</title>
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.2" />
            <stop offset="72%" stopColor={color} stopOpacity="0.045" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        <rect className="np-md-chart-frame" x="54" y="40" width="290" height="142" rx="3" />
        <g className="np-md-chart-grid">
          <line x1="54" y1="40" x2="344" y2="40" />
          <line x1="54" y1="111" x2="344" y2="111" />
          <line x1="54" y1="182" x2="344" y2="182" />
          <line x1="54" y1="40" x2="54" y2="182" />
          <line x1="199" y1="40" x2="199" y2="182" />
          <line x1="344" y1="40" x2="344" y2="182" />
        </g>
        <path
          d={`${geometry.linePath} L ${lastPoint.x.toFixed(2)} 182 L ${firstPoint.x.toFixed(2)} 182 Z`}
          fill={`url(#${gradientId})`}
        />
        <path
          className="np-md-chart-line-glow"
          d={geometry.linePath}
          fill="none"
          stroke={color}
          strokeWidth="6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          className="np-md-chart-line"
          d={geometry.linePath}
          fill="none"
          stroke={color}
          strokeWidth="2.3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <circle className="np-md-chart-endpoint" cx={firstPoint.x} cy={firstPoint.y} r="3" fill="#fff" stroke={color} />
        <circle className="np-md-chart-endpoint is-final" cx={lastPoint.x} cy={lastPoint.y} r="4" fill={color} stroke="#fff" />
        {yTicks.map((tick, index) => (
          <text key={`y-${tick}`} x="48" y={185 - index * 71} textAnchor="end">{formatNumber(tick, 3)}</text>
        ))}
        {xTicks.map((tick, index) => (
          <text key={`x-${tick}`} x={54 + index * 145} y="202" textAnchor={index === 0 ? "start" : index === 2 ? "end" : "middle"}>{formatNumber(tick, 2)}</text>
        ))}
        <text x="344" y="216" textAnchor="end">时间 (ps)</text>
        <text x="8" y="24">{unit}</text>
      </svg>
      <div className="np-md-chart-meta">
        <span>最小值 <strong>{formatNumber(geometry.yMin, 4)} {unit}</strong></span>
        <span>最大值 <strong>{formatNumber(geometry.yMax, 4)} {unit}</strong></span>
        <span className="is-final">最终值 <strong>{formatNumber(finalValue, 4)} {unit}</strong></span>
        <span><strong>{series.points.length}</strong> 个数据点</span>
      </div>
    </div>
  );
}
