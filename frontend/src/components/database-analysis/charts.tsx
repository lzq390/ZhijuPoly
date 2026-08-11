import type { ReactNode } from "react";
import type {
  FormulationAnalytics,
  RangeItem,
  RankedItem,
  StructureEffectAnalytics
} from "./types";

export const DATA_COLORS = ["#3b82f6", "#06a7c5", "#10b981", "#8b5cf6", "#f59e0b", "#64748b", "#4f46e5"];

export function formatNumber(value: number | null | undefined, digits = 1) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: digits }).format(value);
}

export function formatPercent(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${formatNumber(value, 1)}%`;
}

export function formatTimestamp(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  })
    .format(date)
    .replaceAll("/", "-");
}

export function KpiStrip({
  items
}: {
  items: Array<{ label: string; value: string; unit?: string; note?: string }>;
}) {
  return (
    <section className="dba-kpi-strip" aria-label="关键指标">
      {items.map((item) => (
        <div className="dba-kpi-item" key={item.label}>
          <div className="dba-kpi-label">{item.label}</div>
          <div className="dba-kpi-value">
            {item.value}
            {item.unit ? <span className="dba-kpi-unit">{item.unit}</span> : null}
          </div>
          {item.note ? <div className="dba-kpi-note">{item.note}</div> : null}
        </div>
      ))}
    </section>
  );
}

export function Panel({
  title,
  subtitle,
  meta,
  children,
  className = ""
}: {
  title: string;
  subtitle?: string;
  meta?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`dba-panel ${className}`.trim()} aria-label={title}>
      <header className="dba-panel-head">
        <div>
          <h3 className="dba-panel-title">{title}</h3>
          {subtitle ? <p className="dba-panel-subtitle">{subtitle}</p> : null}
        </div>
        {meta ? <span className="dba-panel-meta">{meta}</span> : null}
      </header>
      <div className="dba-panel-body">{children}</div>
    </section>
  );
}

export function EmptyPanel({ message = "当前数据源没有返回可展示的统计结果。" }: { message?: string }) {
  return <div className="dba-panel-empty">{message}</div>;
}

export function BarList({
  data,
  onSelect,
  limit = 6,
  valueFormatter = (value) => formatNumber(value, 0)
}: {
  data: RankedItem[];
  onSelect?: (item: RankedItem, trigger: HTMLButtonElement) => void;
  limit?: number;
  valueFormatter?: (value: number) => string;
}) {
  const visible = data.slice(0, limit);
  if (!visible.length) return <EmptyPanel />;
  const max = Math.max(1, ...visible.map((item) => item.value));

  return (
    <div className="dba-bar-list">
      {visible.map((item, index) => {
        const content = (
          <>
            <span className="dba-bar-label" title={item.label}>{item.label}</span>
            <span className="dba-bar-track">
              <span
                className="dba-bar-fill"
                style={{
                  width: `${Math.max(2, (item.value / max) * 100)}%`,
                  background: item.color ?? DATA_COLORS[index % DATA_COLORS.length]
                }}
              />
            </span>
            <strong className="dba-bar-value">{valueFormatter(item.value)}</strong>
          </>
        );

        return onSelect ? (
          <button
            className="dba-bar-row"
            type="button"
            key={item.label}
            aria-label={`${item.label}，${valueFormatter(item.value)}，打开相关记录`}
            onClick={(event) => onSelect(item, event.currentTarget)}
          >
            {content}
          </button>
        ) : (
          <div className="dba-bar-row" key={item.label}>{content}</div>
        );
      })}
    </div>
  );
}

export function ChipCloud({
  data,
  onSelect,
  limit = 10
}: {
  data: RankedItem[];
  onSelect?: (item: RankedItem, trigger: HTMLButtonElement) => void;
  limit?: number;
}) {
  const visible = data.slice(0, limit);
  if (!visible.length) return <EmptyPanel />;
  return (
    <div className="dba-chip-cloud">
      {visible.map((item) =>
        onSelect ? (
          <button
            type="button"
            className="dba-data-chip"
            key={item.label}
            onClick={(event) => onSelect(item, event.currentTarget)}
          >
            <span>{item.label}</span><strong>{formatNumber(item.value, 0)}</strong>
          </button>
        ) : (
          <span className="dba-data-chip" key={item.label}>
            <span>{item.label}</span><strong>{formatNumber(item.value, 0)}</strong>
          </span>
        )
      )}
    </div>
  );
}

export function DonutBlock({ data, centerLabel = "记录" }: { data: RankedItem[]; centerLabel?: string }) {
  const visible = data.filter((item) => item.value > 0).slice(0, 7);
  const total = visible.reduce((sum, item) => sum + item.value, 0);
  if (!visible.length || total <= 0) return <EmptyPanel />;
  let cursor = 0;
  const segments = visible.map((item, index) => {
    const start = cursor;
    cursor += (item.value / total) * 100;
    return `${item.color ?? DATA_COLORS[index % DATA_COLORS.length]} ${start}% ${cursor}%`;
  });

  return (
    <div className="dba-donut-layout">
      <div
        className="dba-donut"
        style={{ background: `conic-gradient(${segments.join(",")})` }}
        role="img"
        aria-label={visible.map((item) => `${item.label} ${formatPercent((item.value / total) * 100)}`).join("，")}
      >
        <div className="dba-donut-center">
          <strong>{formatNumber(total, 0)}</strong>
          <span>{centerLabel}</span>
        </div>
      </div>
      <div className="dba-legend-list">
        {visible.map((item, index) => (
          <div className="dba-legend-row" key={item.label}>
            <span className="dba-legend-dot" style={{ background: item.color ?? DATA_COLORS[index % DATA_COLORS.length] }} />
            <span title={item.label}>{item.label}</span>
            <strong>{formatPercent((item.value / total) * 100)}</strong>
          </div>
        ))}
      </div>
    </div>
  );
}

export function RangeList({ data, limit = 5 }: { data: RangeItem[]; limit?: number }) {
  const visible = data.slice(0, limit);
  if (!visible.length) return <EmptyPanel />;
  return (
    <div className="dba-range-list">
      {visible.map((item) => {
        const minimum = item.p5 ?? item.min;
        const maximum = item.p95 ?? item.max;
        const span = maximum - minimum || 1;
        const median = Math.min(100, Math.max(0, ((item.median - minimum) / span) * 100));
        return (
          <div className="dba-range-row" key={item.label}>
            <span className="dba-range-label" title={item.label}>{item.label}</span>
            <span className="dba-range-track">
              <span className="dba-range-span" />
              <span className="dba-range-marker" style={{ left: `${median}%` }} />
            </span>
            <span className="dba-range-value">
              {formatNumber(minimum)} — {formatNumber(maximum)}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function SourceMatrix({ data }: { data: StructureEffectAnalytics["sourceMatrix"] }) {
  if (!data.length) return <EmptyPanel />;
  const visible = data.slice(0, 5);
  const max = Math.max(1, ...visible.flatMap((item) => [item.exp, item.sim, item.na]));
  return (
    <div className="dba-matrix-wrap">
      <div className="dba-matrix">
        <span />
        <span className="dba-matrix-head">实验</span>
        <span className="dba-matrix-head">模拟</span>
        <span className="dba-matrix-head">未标注</span>
        {visible.map((row) => (
          <div className="dba-matrix-row" key={row.label}>
            <span className="dba-matrix-label" title={row.label}>{row.label}</span>
            {(["exp", "sim", "na"] as const).map((key) => (
              <span
                className="dba-matrix-cell"
                key={key}
                style={{ backgroundColor: `rgba(139, 92, 246, ${0.07 + (row[key] / max) * 0.42})` }}
              >
                {formatNumber(row[key], 0)}
              </span>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function CoverageList({ data }: { data: FormulationAnalytics["coverage"] }) {
  if (!data.length) return <EmptyPanel />;
  return (
    <div className="dba-coverage-list">
      {data.slice(0, 6).map((item) => (
        <div className="dba-coverage-row" key={item.label}>
          <span>{item.label}</span>
          <span className="dba-coverage-track"><span style={{ width: `${Math.max(2, item.pct)}%` }} /></span>
          <strong>{formatPercent(item.pct)}</strong>
        </div>
      ))}
    </div>
  );
}

export function DistributionGroups({
  groups
}: {
  groups: Array<{ label: string; data: RankedItem[] }>;
}) {
  const nonEmpty = groups.filter((group) => group.data.length);
  if (!nonEmpty.length) return <EmptyPanel />;
  return (
    <div className="dba-distribution-groups">
      {nonEmpty.map((group) => (
        <section key={group.label}>
          <h4>{group.label}</h4>
          <div>
            {group.data.slice(0, 5).map((item) => (
              <span className="dba-distribution-chip" key={item.label}>
                <span>{item.label}</span><strong>{formatNumber(item.value, 0)}</strong>
              </span>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

export function DataTable({
  caption,
  headers,
  rows
}: {
  caption: string;
  headers: string[];
  rows: ReactNode[][];
}) {
  if (!rows.length) return <EmptyPanel />;
  return (
    <div className="dba-table-wrap">
      <table>
        <caption className="dba-sr-only">{caption}</caption>
        <thead><tr>{headers.map((header) => <th scope="col" key={header}>{header}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => <td className={cellIndex === 0 ? "dba-cell-main" : undefined} key={cellIndex}>{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function averageComponentCount(data: RankedItem[]) {
  const total = data.reduce((sum, item) => sum + item.value, 0);
  if (!total) return null;
  const weighted = data.reduce((sum, item) => {
    const value = item.label === "8+" ? 8 : Number.parseInt(item.label, 10);
    return sum + (Number.isFinite(value) ? value : 0) * item.value;
  }, 0);
  return weighted / total;
}
